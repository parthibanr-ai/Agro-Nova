import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:http_parser/http_parser.dart';

import 'offline.dart';

class ApiException implements Exception {
  ApiException(this.status, this.message);
  final int status;
  final String message;
  @override
  String toString() => message;
}

/// Thin client for the AgriN FastAPI backend. Every request carries `lang` so the server translates
/// dynamic content (advice, remedies, schemes) into the farmer's language.
class Api {
  Api({String? baseUrl, this.devUser, this.tokenProvider, this.appCheckTokenProvider, this.store})
      : baseUrl = baseUrl ??
            const String.fromEnvironment('API_BASE_URL',
                defaultValue: kIsWeb ? 'http://localhost:8000' : 'http://10.0.2.2:8000');

  // A stalled request must not spin forever on a weak rural connection. Reads get longer than writes because
  // the AI screens can legitimately take a while server-side.
  static const _readTimeout = Duration(seconds: 45);
  static const _writeTimeout = Duration(seconds: 30);
  static const _uploadTimeout = Duration(seconds: 90);
  /// How many times a GET is repeated when the connection itself failed (tests set 0 to run fast).
  int connectRetries = 2;
  static const offlineMessage = 'No connection. Check your network and try again.';
  static const _slowMessage = 'The server took too long to answer. Please try again.';
  final _rng = Random();

  final String baseUrl;
  final String? devUser; // AUTH_MODE=dev on the server
  /// Answers seen before, used when the server cannot be reached (null: no offline copy, e.g. in tests).
  final OfflineStore? store;

  /// True after a request failed for lack of connection, false again after the next one gets an answer. The
  /// app shows a banner from it.
  final ValueNotifier<bool> offline = ValueNotifier(false);

  /// True while the screen in front of the farmer is showing a saved answer instead of a fresh one.
  final ValueNotifier<bool> showingSaved = ValueNotifier(false);

  /// Called when the server says the farmer has not agreed to the privacy notice ("Consent required: ..."), so the
  /// app can show it.
  void Function()? onConsentRequired;
  final Future<String?> Function()? tokenProvider; // Firebase ID token in production
  /// Firebase App Check token: proves the request comes from the genuine app, not a script (see the server's
  /// APP_CHECK_MODE). Null when App Check is not set up.
  final Future<String?> Function()? appCheckTokenProvider;
  String lang = 'en';

  Future<Map<String, String>> _headers({bool json = true}) async {
    final h = <String, String>{if (json) 'Content-Type': 'application/json'};
    final token = await tokenProvider?.call();
    if (token != null) h['Authorization'] = 'Bearer $token';
    try {
      final appCheck = await appCheckTokenProvider?.call();
      if (appCheck != null) h['X-Firebase-AppCheck'] = appCheck;
    } catch (_) {} // no token: the server decides (it only refuses in enforce mode)
    if (devUser != null) h['X-Dev-User'] = devUser!;
    return h;
  }

  Uri _uri(String path, [Map<String, String>? query]) =>
      Uri.parse('$baseUrl/api/v1$path').replace(queryParameters: {...?query, 'lang': lang});

  dynamic _decode(http.Response r) {
    if (r.statusCode >= 200 && r.statusCode < 300) {
      return r.body.isEmpty ? null : jsonDecode(utf8.decode(r.bodyBytes));
    }
    var msg = 'Request failed (${r.statusCode})';
    try {
      final d = jsonDecode(utf8.decode(r.bodyBytes))['detail'];
      if (d is String) msg = d;
    } catch (_) {}
    if (r.statusCode == 403 && msg.startsWith('Consent required')) onConsentRequired?.call();
    throw ApiException(r.statusCode, msg);
  }

  /// Sends one request with a timeout, turning network failures into an [ApiException] the screens can show.
  Future<http.Response> _send(Future<http.Response> Function() call, Duration timeout) async {
    try {
      final r = await call().timeout(timeout);
      offline.value = false; // any answer, even an error, proves the connection works
      return r;
    } on TimeoutException {
      throw ApiException(0, _slowMessage);
    } on http.ClientException {
      offline.value = true;
      throw ApiException(0, offlineMessage);
    }
  }

  /// GETs are safe to repeat, so retry when the connection itself failed (weak signal, brief drop) with
  /// exponential backoff plus jitter, so thousands of phones reconnecting together do not hit the server in
  /// lockstep. Timeouts and server errors are deliberately NOT retried automatically: repeating a slow AI
  /// request would add load exactly when the server is struggling. The screen's "Try again" button covers it.
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    final key = _cacheKey('GET', path, query);
    try {
      final data = await _getFromServer(path, query);
      showingSaved.value = false;
      if (_worthSaving(path)) await _remember(key, data);
      return data;
    } on ApiException catch (e) {
      final saved = e.status == 0 ? store?.get(key) : null;
      if (saved == null) rethrow;
      showingSaved.value = true; // no signal: show what the phone saw last time rather than an error
      return saved.data;
    }
  }

  // The answer to the same question, for a farmer using the same language. Nothing here identifies the farmer:
  // the store lives on their own phone and is wiped when they delete their data.
  String _cacheKey(String method, String path, Map<String, String>? query) {
    final q = {...?query, 'lang': lang};
    final parts = (q.keys.toList()..sort()).map((k) => '$k=${q[k]}').join('&');
    return '$method $path?$parts';
  }

  bool _worthSaving(String path) => !path.startsWith('/jobs/') && path != '/me/export' && path != '/geocode';

  Future<void> _remember(String key, dynamic data) async {
    try {
      await store?.put(key, data);
    } catch (_) {} // a full disk must never break a working screen
  }

  Future<dynamic> _getFromServer(String path, Map<String, String>? query) async {
    for (var attempt = 0;; attempt++) {
      try {
        return _decode(await _send(
            () async => http.get(_uri(path, query), headers: await _headers()), _readTimeout));
      } on ApiException catch (e) {
        if (e.status != 0 || e.message != offlineMessage || attempt >= connectRetries) rethrow;
        final base = 1000 * pow(2, attempt).toInt(); // 1 s, 2 s
        await Future<void>.delayed(Duration(milliseconds: base + _rng.nextInt(base)));
      }
    }
  }

  // Writes are not retried automatically: repeating a POST could create a plot twice.
  Future<dynamic> post(String path, Map<String, dynamic> body) async => _decode(await _send(
      () async => http.post(_uri(path), headers: await _headers(), body: jsonEncode(body)), _writeTimeout));

  /// Sends a queued change (see [WriteQueue]) with its original method.
  Future<dynamic> write(String method, String path, Map<String, dynamic> body) =>
      method == 'PUT' ? put(path, body) : post(path, body);

  Future<dynamic> put(String path, Map<String, dynamic> body) async => _decode(await _send(
      () async => http.put(_uri(path), headers: await _headers(), body: jsonEncode(body)), _writeTimeout));

  Future<dynamic> delete(String path, {Map<String, String>? query}) async => _decode(
      await _send(() async => http.delete(_uri(path, query), headers: await _headers()), _writeTimeout));

  // ---- Slow AI requests run as server-side jobs -------------------------------------------------------------
  // The server answers at once with a job id (or with the finished answer, if it was already cached), and the
  // phone polls a cheap status endpoint. That keeps one slow Gemini call from tying up a connection on a weak
  // network, and lets the server absorb a burst by queueing instead of timing out.

  static const _pollBudget = Duration(seconds: 120);

  /// Wait before poll number [attempt]: 1 s, 1.5 s, 2 s ... capped at 3 s, plus jitter so phones do not poll in step.
  Duration Function(int attempt) pollDelay = (attempt) {
    final ms = min(3000, 1000 + attempt * 500);
    return Duration(milliseconds: ms + Random().nextInt(300));
  };

  /// Crop recommendation for a plot. Uses the job endpoints; an older server without them is served by the
  /// direct endpoint instead.
  Future<dynamic> cropRecommendation(String plotId) => _runJob(
        cacheKey: _cacheKey('JOB', '/plots/$plotId/crop-recommendation', null),
        start: () async => http.post(_uri('/plots/$plotId/crop-recommendation/jobs'), headers: await _headers()),
        startTimeout: _writeTimeout,
        fallback: () => get('/plots/$plotId/crop-recommendation'),
      );

  // The other slow screens work the same way. Each falls back to its direct GET on a server without job endpoints.
  Future<dynamic> _jobOrGet(String jobPath, String directPath, [Map<String, String>? query]) => _runJob(
        cacheKey: _cacheKey('JOB', directPath, query),
        start: () async => http.post(_uri(jobPath, query), headers: await _headers()),
        startTimeout: _writeTimeout,
        fallback: () => get(directPath, query: query),
      );

  Future<dynamic> resilience({String? plotId}) => _jobOrGet(
      '/resilience/jobs', '/resilience', {if (plotId != null) 'plot_id': plotId});

  Future<dynamic> waterTips({String? plotId}) => _jobOrGet(
      '/water-tips/jobs', '/water-tips', {if (plotId != null) 'plot_id': plotId});

  Future<dynamic> market(String plotId) => _jobOrGet('/plots/$plotId/market/jobs', '/plots/$plotId/market');

  Future<dynamic> forecast(String plotId, {int days = 10}) => _jobOrGet(
      '/plots/$plotId/forecast/jobs', '/plots/$plotId/forecast', {'days': '$days'});

  /// Jobs this phone is currently waiting for. When the app is sent to the background the server is asked to
  /// push "your answer is ready" for each of them, so the farmer can leave and come back (see [notifyPendingJobs]).
  final Set<String> _pendingJobs = {};

  /// Ask the server to send a push notification when each job still being waited on finishes. Best effort: a
  /// failure here only means no push.
  Future<void> notifyPendingJobs() async {
    for (final id in _pendingJobs.toList()) {
      try {
        await post('/jobs/$id/notify', const {});
      } catch (_) {}
    }
  }

  Future<dynamic> diagnose(Uint8List bytes, String filename, {String? crop, String? plotId, String? notes}) async {
    Future<http.Response> send(String path) async {
      final req = http.MultipartRequest('POST', _uri(path))
        ..headers.addAll(await _headers(json: false))
        ..files.add(http.MultipartFile.fromBytes('image', bytes,
            filename: filename,
            contentType: MediaType('image', filename.toLowerCase().endsWith('png') ? 'png' : 'jpeg')));
      if (crop != null) req.fields['crop'] = crop;
      if (plotId != null) req.fields['plot_id'] = plotId;
      if (notes != null) req.fields['notes'] = notes;
      return http.Response.fromStream(await req.send());
    }

    return _runJob(
      cacheKey: null, // a photo answer is not worth keeping: the next photo is a new question
      start: () => send('/diagnosis/jobs'),
      startTimeout: _uploadTimeout,
      fallback: () async => _decode(await _send(() => send('/diagnosis'), _uploadTimeout)),
    );
  }

  Future<dynamic> _runJob({
    required String? cacheKey,
    required Future<http.Response> Function() start,
    required Duration startTimeout,
    required Future<dynamic> Function() fallback,
  }) async {
    try {
      final r = await _send(start, startTimeout);
      final result = (r.statusCode == 404 && _detail(r) == 'Not Found')
          ? await fallback() // server predates the job endpoints
          : await _finishJob(Map<String, dynamic>.from(_decode(r) as Map));
      showingSaved.value = false;
      if (cacheKey != null) await _remember(cacheKey, result);
      return result;
    } on ApiException catch (e) {
      final saved = (e.status == 0 && cacheKey != null) ? store?.get(cacheKey) : null;
      if (saved == null) rethrow;
      showingSaved.value = true;
      return saved.data;
    }
  }

  /// The result of a job that finished while the farmer was away (they tapped its "ready" notification). Throws
  /// [ApiException] 404 once the server has forgotten it (about an hour), and the screen then asks afresh.
  Future<dynamic> jobResult(String jobId) async =>
      _finishJob(Map<String, dynamic>.from(await get('/jobs/$jobId') as Map));

  /// Polls until the job is done. A failed job becomes an [ApiException] with the same status and message the
  /// direct endpoint would have produced, so screens handle one kind of error.
  Future<dynamic> _finishJob(Map<String, dynamic> view) async {
    final started = DateTime.now();
    final jobId = view['job_id'] as String?;
    try {
      for (var attempt = 0;; attempt++) {
        switch (view['status']) {
          case 'done':
            return view['result'];
          case 'failed':
            final e = (view['error'] as Map?) ?? const {};
            throw ApiException((e['status'] as num?)?.toInt() ?? 500, (e['message'] as String?) ?? 'Request failed');
        }
        if (jobId != null) _pendingJobs.add(jobId);
        if (DateTime.now().difference(started) > _pollBudget) throw ApiException(0, _slowMessage);
        await Future<void>.delayed(pollDelay(attempt));
        view = Map<String, dynamic>.from(await get('/jobs/${view['job_id']}') as Map);
      }
    } finally {
      if (jobId != null) _pendingJobs.remove(jobId);
    }
  }

  String? _detail(http.Response r) {
    try {
      final d = jsonDecode(utf8.decode(r.bodyBytes))['detail'];
      return d is String ? d : null;
    } catch (_) {
      return null;
    }
  }
}
