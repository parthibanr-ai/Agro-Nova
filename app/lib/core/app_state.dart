import 'dart:async';
import 'dart:math';

import 'package:flutter/widgets.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api.dart';
import 'offline.dart';

class Plot {
  Plot.fromJson(Map<String, dynamic> j)
      : id = j['id'],
        name = j['name'],
        crop = j['crop'],
        country = j['country'],
        areaAcres = (j['area_acres'] as num).toDouble(),
        lat = (j['centroid']['lat'] as num).toDouble(),
        lon = (j['centroid']['lon'] as num).toDouble(),
        corners = [for (final c in j['corners']) [(c['lat'] as num).toDouble(), (c['lon'] as num).toDouble()]],
        clientRef = j['client_ref'],
        pending = false;

  /// A plot the farmer drew with no connection. It shows in the list at once and reaches the server later.
  Plot.pending(QueuedWrite w)
      : id = 'local-${w.id}',
        name = w.body['name'],
        crop = w.body['crop'],
        country = w.body['country'],
        corners = [for (final c in w.body['corners']) [(c['lat'] as num).toDouble(), (c['lon'] as num).toDouble()]],
        clientRef = w.id,
        pending = true,
        lat = _mean([for (final c in w.body['corners']) (c['lat'] as num).toDouble()]),
        lon = _mean([for (final c in w.body['corners']) (c['lon'] as num).toDouble()]),
        areaAcres = approxAcres([
          for (final c in w.body['corners']) [(c['lat'] as num).toDouble(), (c['lon'] as num).toDouble()]
        ]);

  final String id, name, crop, country;
  final double areaAcres, lat, lon;
  final List<List<double>> corners;
  final String? clientRef;
  final bool pending; // still waiting to be sent to the server
}

double _mean(List<double> v) => v.reduce((a, b) => a + b) / v.length;

/// Rough area of a small polygon, good enough to label a plot until the server (which measures it precisely) has it.
double approxAcres(List<List<double>> c) {
  final lat0 = _mean([for (final p in c) p[0]]);
  const mPerDegLat = 111320.0;
  final mPerDegLon = mPerDegLat * cos(lat0 * pi / 180);
  var twice = 0.0;
  for (var i = 0; i < c.length; i++) {
    final a = c[i], b = c[(i + 1) % c.length];
    twice += (a[1] * mPerDegLon) * (b[0] * mPerDegLat) - (b[1] * mPerDegLon) * (a[0] * mPerDegLat);
  }
  return (twice.abs() / 2 / 4046.856 * 100).round() / 100;
}

class AppState extends ChangeNotifier with WidgetsBindingObserver {
  AppState(this.api, {this.queue});
  final Api api;

  /// Changes made with no connection (null in tests that do not need it).
  final WriteQueue? queue;

  /// A farmer who leaves the app while an answer is still being prepared gets a push when it is ready. Coming back
  /// is the moment to send anything saved while offline.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused) api.notifyPendingJobs();
    if (state == AppLifecycleState.resumed) syncPending();
  }

  String language = 'en';
  List<Plot> plots = [];
  Plot? selected;
  bool loading = false;
  String? error;

  // ---- Privacy notice and consent ------------------------------------------------------------------------
  /// False until the app knows whether the farmer has agreed to the notice (so the notice never flashes up for a
  /// farmer who already agreed, and the home screen never flashes up for one who has not).
  bool ready = false;
  bool needsConsent = false;
  Map<String, dynamic>? consent;

  /// Called when the farmer allows notifications, to register this phone for push (set in main.dart).
  Future<void> Function()? onNotificationsAllowed;

  bool consented(String purpose) => (consent?['purposes'] as Map?)?[purpose] == true;

  Future<void> loadConsent() async {
    final prefs = await SharedPreferences.getInstance();
    try {
      consent = Map<String, dynamic>.from(await api.get('/me/consent').timeout(const Duration(seconds: 8)) as Map);
      needsConsent = consent!['needs_consent'] == true;
    } catch (_) {
      // Cannot ask the server (no signal): go by what this phone remembers. A farmer who never agreed still sees it.
      needsConsent = prefs.getString('consent_version') == null;
    }
  }

  /// The privacy notice text (also kept for offline reading).
  Future<Map<String, dynamic>> loadNotice() async => Map<String, dynamic>.from(await api.get('/consent/notice') as Map);

  /// Records the farmer's choices for [noticeVersion]. Throws [ApiException] (409) if the notice changed meanwhile.
  Future<void> acceptConsent(String noticeVersion, Map<String, bool> purposes) async {
    consent = Map<String, dynamic>.from(await api.put('/me/consent', {'notice_version': noticeVersion, 'purposes': purposes}) as Map);
    needsConsent = consent!['needs_consent'] == true;
    (await SharedPreferences.getInstance()).setString('consent_version', noticeVersion);
    notifyListeners();
    if (consented('notifications')) {
      try {
        await onNotificationsAllowed?.call();
      } catch (_) {}
    }
  }

  // ---- Start-up ------------------------------------------------------------------------------------------
  Timer? _syncTimer;

  Future<void> init() async {
    WidgetsBinding.instance.addObserver(this);
    final prefs = await SharedPreferences.getInstance();
    language = prefs.getString('lang') ?? 'en';
    api.lang = language;
    api.onConsentRequired = () {
      needsConsent = true;
      notifyListeners();
    };
    api.offline.addListener(_connectivityChanged);
    await loadConsent();
    ready = true;
    notifyListeners();
    if (!needsConsent && consented('notifications')) {
      try {
        await onNotificationsAllowed?.call();
      } catch (_) {}
    }
    await loadPlots();
    _syncTimer = Timer.periodic(const Duration(seconds: 45), (_) => syncPending());
    unawaited(syncPending());
  }

  @override
  void dispose() {
    _syncTimer?.cancel();
    api.offline.removeListener(_connectivityChanged);
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  void _connectivityChanged() {
    if (!api.offline.value) syncPending();
  }

  Future<void> setLanguage(String code) async {
    language = code;
    api.lang = code;
    notifyListeners();
    try {
      (await SharedPreferences.getInstance()).setString('lang', code);
      await api.put('/me', {'language': code});
    } on ApiException catch (e) {
      if (e.status == 0) {
        // Offline: the choice is already saved on the phone; tell the server when there is a connection.
        await queue?.add(QueuedWrite(id: 'profile', kind: 'profile', method: 'PUT', path: '/me', body: {'language': code}));
        notifyListeners();
      }
    } catch (_) {}
  }

  Future<void> loadPlots() async {
    loading = true;
    error = null;
    notifyListeners();
    var fromServer = <Plot>[];
    try {
      final data = await api.get('/plots');
      fromServer = [for (final p in data['plots']) Plot.fromJson(p)];
    } catch (e) {
      error = e.toString();
    }
    final known = {for (final p in fromServer) p.clientRef};
    final waiting = [
      for (final w in queue?.items ?? const <QueuedWrite>[])
        if (w.kind == 'plot' && !known.contains(w.id)) Plot.pending(w)
    ];
    plots = [...fromServer, ...waiting];
    selected = plots.isEmpty ? null : plots.firstWhere((p) => p.id == selected?.id, orElse: () => plots.first);
    loading = false;
    notifyListeners();
  }

  /// Called after the server has erased the account, so the app can drop its sign-in (see main.dart).
  Future<void> Function()? onAccountDeleted;

  /// Permanently erases this farmer's data on the server, then forgets everything held on the phone.
  Future<void> deleteAccount() async {
    await api.delete('/me', query: {'confirm': 'true'});
    plots = [];
    selected = null;
    error = null;
    await api.store?.clear();
    await queue?.clear();
    (await SharedPreferences.getInstance()).remove('consent_version');
    consent = null;
    needsConsent = true; // the fresh identity that follows has agreed to nothing yet
    notifyListeners();
    await onAccountDeleted?.call();
  }

  void select(Plot p) {
    selected = p;
    notifyListeners();
  }

  static final _rng = Random.secure();
  static String _newRef() =>
      '${DateTime.now().microsecondsSinceEpoch.toRadixString(36)}-${_rng.nextInt(1 << 32).toRadixString(36)}';

  /// Creates the plot; throws [ApiException] with the server's validation message on bad geometry. With no
  /// connection the plot is kept on the phone and sent later, and is returned marked [Plot.pending].
  Future<Plot> createPlot({
    required String name,
    required String crop,
    required String country,
    String? state,
    DateTime? sowingDate,
    required List<List<double>> corners,
  }) async {
    final ref = _newRef(); // lets the server recognise a retry of this same plot
    final body = {
      'name': name,
      'crop': crop,
      'country': country,
      if (state != null && state.isNotEmpty) 'state': state,
      if (sowingDate != null) 'sowing_date': sowingDate.toIso8601String().substring(0, 10),
      'corners': [for (final c in corners) {'lat': c[0], 'lon': c[1]}],
      'client_ref': ref,
    };
    Plot plot;
    try {
      plot = Plot.fromJson(await api.post('/plots', body));
    } on ApiException catch (e) {
      // Only "could not reach the server" is queued. A refusal (bad geometry, no consent) is for the farmer to see.
      if (e.status != 0 || queue == null) rethrow;
      final w = QueuedWrite(id: ref, kind: 'plot', method: 'POST', path: '/plots', body: body);
      await queue!.add(w);
      plot = Plot.pending(w);
    }
    plots = [...plots, plot];
    selected = plot;
    notifyListeners();
    return plot;
  }

  /// Saves a soil sample. Returns true when the server has it, false when it was kept on the phone to send later.
  Future<bool> saveSoilSample(Plot plot, Map<String, dynamic> body) async {
    final ref = _newRef();
    final withRef = {...body, 'client_ref': ref};
    if (!plot.pending) {
      try {
        await api.post('/plots/${plot.id}/soil', withRef);
        return true;
      } on ApiException catch (e) {
        if (e.status != 0 || queue == null) rethrow;
      }
    }
    await queue!.add(QueuedWrite(
      id: ref,
      kind: 'soil',
      method: 'POST',
      path: plot.pending ? '/plots/{plot}/soil' : '/plots/${plot.id}/soil',
      body: withRef,
      plotRef: plot.pending ? plot.clientRef : null,
    ));
    notifyListeners();
    return false;
  }

  // ---- Sending what was saved offline --------------------------------------------------------------------
  bool _syncing = false;

  /// Something the server refused for good while syncing (shown once, then cleared with [clearSyncProblem]).
  String? syncProblem;
  void clearSyncProblem() {
    syncProblem = null;
    notifyListeners();
  }

  int get pendingCount => queue?.length ?? 0;
  final Map<String, String> _plotIds = {}; // client_ref -> server id, for writes that belong to a just-synced plot

  String? _serverPlotId(String ref) =>
      _plotIds[ref] ?? plots.where((p) => !p.pending && p.clientRef == ref).map((p) => p.id).firstOrNull;

  /// Sends the queued changes in order. Stops at the first sign of no connection or a busy server and tries again
  /// later (a timer, coming back to the app, or the connection returning). Safe to call at any time and to repeat:
  /// the server recognises a retried plot or sample by its client_ref.
  Future<void> syncPending() async {
    final q = queue;
    if (_syncing || q == null || q.length == 0) return;
    _syncing = true;
    var sent = false;
    try {
      for (final w in q.items) {
        var path = w.path;
        if (w.plotRef != null) {
          final id = _serverPlotId(w.plotRef!);
          if (id == null) continue; // its plot has not reached the server yet
          path = path.replaceFirst('{plot}', id);
        }
        try {
          final r = await api.write(w.method, path, w.body);
          if (w.kind == 'plot' && r is Map && r['id'] != null) _plotIds[w.id] = r['id'] as String;
          await q.remove(w.id);
          sent = true;
        } on ApiException catch (e) {
          if (e.status == 0 || e.status == 429 || e.status >= 500) break; // try again later
          if (e.status == 403 && e.message.startsWith('Consent required')) break; // waits for the farmer's consent
          await q.remove(w.id); // the server refused it for good; keeping it would block everything behind it
          syncProblem = e.message;
        }
      }
    } finally {
      _syncing = false;
    }
    if (sent) {
      await loadPlots();
    } else {
      notifyListeners();
    }
  }
}
