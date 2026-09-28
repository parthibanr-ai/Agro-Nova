import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:http_parser/http_parser.dart';

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
  Api({String? baseUrl, this.devUser, this.tokenProvider})
      : baseUrl = baseUrl ??
            const String.fromEnvironment('API_BASE_URL',
                defaultValue: kIsWeb ? 'http://localhost:8000' : 'http://10.0.2.2:8000');

  // A stalled request must not spin forever on a weak rural connection. Reads get longer than writes because
  // the AI screens can legitimately take a while server-side.
  static const _readTimeout = Duration(seconds: 45);
  static const _writeTimeout = Duration(seconds: 30);
  static const _uploadTimeout = Duration(seconds: 90);
  static const _maxConnectRetries = 2;
  static const _offlineMessage = 'No connection. Check your network and try again.';
  static const _slowMessage = 'The server took too long to answer. Please try again.';
  final _rng = Random();

  final String baseUrl;
  final String? devUser; // AUTH_MODE=dev on the server
  final Future<String?> Function()? tokenProvider; // Firebase ID token in production
  String lang = 'en';

  Future<Map<String, String>> _headers({bool json = true}) async {
    final h = <String, String>{if (json) 'Content-Type': 'application/json'};
    final token = await tokenProvider?.call();
    if (token != null) h['Authorization'] = 'Bearer $token';
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
    throw ApiException(r.statusCode, msg);
  }

  /// Sends one request with a timeout, turning network failures into an [ApiException] the screens can show.
  Future<http.Response> _send(Future<http.Response> Function() call, Duration timeout) async {
    try {
      return await call().timeout(timeout);
    } on TimeoutException {
      throw ApiException(0, _slowMessage);
    } on http.ClientException {
      throw ApiException(0, _offlineMessage);
    }
  }

  /// GETs are safe to repeat, so retry when the connection itself failed (weak signal, brief drop) with
  /// exponential backoff plus jitter, so thousands of phones reconnecting together do not hit the server in
  /// lockstep. Timeouts and server errors are deliberately NOT retried automatically: repeating a slow AI
  /// request would add load exactly when the server is struggling. The screen's "Try again" button covers it.
  Future<dynamic> get(String path, {Map<String, String>? query}) async {
    for (var attempt = 0;; attempt++) {
      try {
        return _decode(await _send(
            () async => http.get(_uri(path, query), headers: await _headers()), _readTimeout));
      } on ApiException catch (e) {
        if (e.status != 0 || e.message != _offlineMessage || attempt >= _maxConnectRetries) rethrow;
        final base = 1000 * pow(2, attempt).toInt(); // 1 s, 2 s
        await Future<void>.delayed(Duration(milliseconds: base + _rng.nextInt(base)));
      }
    }
  }

  // Writes are not retried automatically: repeating a POST could create a plot twice.
  Future<dynamic> post(String path, Map<String, dynamic> body) async => _decode(await _send(
      () async => http.post(_uri(path), headers: await _headers(), body: jsonEncode(body)), _writeTimeout));

  Future<dynamic> put(String path, Map<String, dynamic> body) async => _decode(await _send(
      () async => http.put(_uri(path), headers: await _headers(), body: jsonEncode(body)), _writeTimeout));

  Future<dynamic> delete(String path) async => _decode(
      await _send(() async => http.delete(_uri(path), headers: await _headers()), _writeTimeout));

  Future<dynamic> diagnose(Uint8List bytes, String filename, {String? crop, String? plotId, String? notes}) async {
    final req = http.MultipartRequest('POST', _uri('/diagnosis'))
      ..headers.addAll(await _headers(json: false))
      ..files.add(http.MultipartFile.fromBytes('image', bytes,
          filename: filename,
          contentType: MediaType('image', filename.toLowerCase().endsWith('png') ? 'png' : 'jpeg')));
    if (crop != null) req.fields['crop'] = crop;
    if (plotId != null) req.fields['plot_id'] = plotId;
    if (notes != null) req.fields['notes'] = notes;
    return _decode(await _send(() async => http.Response.fromStream(await req.send()), _uploadTimeout));
  }
}
