import 'dart:convert';

import 'package:agrin/core/api.dart';
import 'package:agrin/core/app_state.dart';
import 'package:agrin/core/offline.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(jsonEncode(body), status,
    headers: {'content-type': 'application/json; charset=utf-8'});

const _square = [
  {'lat': 19.9975, 'lon': 73.7898},
  {'lat': 19.9975, 'lon': 73.7908},
  {'lat': 19.9985, 'lon': 73.7908},
  {'lat': 19.9985, 'lon': 73.7898},
];

Map<String, dynamic> _serverPlot(String id, String? ref) => {
      'id': id,
      'name': 'North field',
      'crop': 'wheat',
      'country': 'IN',
      'area_acres': 6.2,
      'centroid': {'lat': 19.998, 'lon': 73.7903},
      'corners': _square,
      'client_ref': ref,
    };

/// A pretend server the test can switch offline. [seen] records what reached it.
class FakeServer {
  bool online = true;
  final List<String> seen = [];
  final List<Map<String, dynamic>> plotBodies = [];
  Future<http.Response> Function(http.Request req)? respond;

  Future<http.Response> handle(http.Request req) async {
    if (!online) throw http.ClientException('no route to host');
    seen.add('${req.method} ${req.url.path}');
    if (req.method == 'POST' && req.url.path == '/api/v1/plots') plotBodies.add(jsonDecode(req.body));
    return respond != null ? respond!(req) : _json({});
  }
}

Future<T> withServer<T>(FakeServer s, Future<T> Function() body) =>
    http.runWithClient(body, () => MockClient(s.handle));

Future<(Api, OfflineStore, WriteQueue)> _setup() async {
  SharedPreferences.setMockInitialValues({});
  final prefs = await SharedPreferences.getInstance();
  final store = OfflineStore(prefs), queue = WriteQueue(prefs);
  final api = Api(baseUrl: 'http://test', devUser: 'tester', store: store)
    ..connectRetries = 0
    ..pollDelay = (_) => Duration.zero;
  return (api, store, queue);
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('OfflineStore', () {
    test('keeps an answer with the time it was saved, and forgets damaged entries', () async {
      SharedPreferences.setMockInitialValues({'oc:bad': 'not json'});
      final store = OfflineStore(await SharedPreferences.getInstance());
      await store.put('k', {'a': 1});
      expect(store.get('k')!.data, {'a': 1});
      expect(DateTime.now().toUtc().difference(store.get('k')!.savedAt).inSeconds, lessThan(5));
      expect(store.get('bad'), isNull);
      expect(store.get('missing'), isNull);
    });

    test('is bounded: the least recently saved entries go first, and huge answers are not kept', () async {
      SharedPreferences.setMockInitialValues({});
      final store = OfflineStore(await SharedPreferences.getInstance());
      for (var i = 0; i < OfflineStore.maxEntries + 5; i++) {
        await store.put('k$i', i);
      }
      expect(store.get('k0'), isNull);
      expect(store.get('k4'), isNull);
      expect(store.get('k5')!.data, 5);
      expect(store.get('k${OfflineStore.maxEntries + 4}')!.data, OfflineStore.maxEntries + 4);
      await store.put('big', 'x' * (OfflineStore.maxEntryBytes + 1));
      expect(store.get('big'), isNull);
    });

    test('clear removes everything', () async {
      SharedPreferences.setMockInitialValues({});
      final store = OfflineStore(await SharedPreferences.getInstance());
      await store.put('a', 1);
      await store.clear();
      expect(store.get('a'), isNull);
    });
  });

  group('WriteQueue', () {
    test('survives an app restart and keeps order', () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final q = WriteQueue(prefs);
      await q.add(QueuedWrite(id: 'a', kind: 'plot', method: 'POST', path: '/plots', body: {'n': 1}));
      await q.add(QueuedWrite(id: 'b', kind: 'soil', method: 'POST', path: '/x', body: {'n': 2}, plotRef: 'a'));
      final again = WriteQueue(prefs);
      expect(again.items.map((w) => w.id), ['a', 'b']);
      expect(again.items[1].plotRef, 'a');
      await again.remove('a');
      expect(WriteQueue(prefs).items.map((w) => w.id), ['b']);
    });

    test('only the latest profile change is kept, and a damaged queue starts empty', () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final q = WriteQueue(prefs);
      await q.add(QueuedWrite(id: 'profile', kind: 'profile', method: 'PUT', path: '/me', body: {'language': 'hi'}));
      await q.add(QueuedWrite(id: 'profile', kind: 'profile', method: 'PUT', path: '/me', body: {'language': 'ta'}));
      expect(q.items.length, 1);
      expect(q.items.single.body['language'], 'ta');
      SharedPreferences.setMockInitialValues({'wq:items': '{{{'});
      expect(WriteQueue(await SharedPreferences.getInstance()).length, 0);
    });
  });

  group('Api offline behaviour', () {
    test('a screen still opens offline with the answer it last saw, and says so', () async {
      final (api, _, _) = await _setup();
      final server = FakeServer()..respond = (_) async => _json({'plots': ['p1']});
      await withServer(server, () async {
        expect((await api.get('/plots'))['plots'], ['p1']);
        expect(api.showingSaved.value, isFalse);
        server.online = false;
        expect((await api.get('/plots'))['plots'], ['p1']);
        expect(api.showingSaved.value, isTrue);
        expect(api.offline.value, isTrue);
        server.online = true;
        await api.get('/plots');
        expect(api.showingSaved.value, isFalse);
        expect(api.offline.value, isFalse);
      });
    });

    test('with nothing saved, offline is an error the screen can show', () async {
      final (api, _, _) = await _setup();
      final server = FakeServer()..online = false;
      await withServer(server, () async {
        await expectLater(api.get('/plots'), throwsA(isA<ApiException>().having((e) => e.message, 'm', Api.offlineMessage)));
      });
    });

    test('saved answers are per language', () async {
      final (api, _, _) = await _setup();
      final server = FakeServer()..respond = (req) async => _json({'lang': req.url.queryParameters['lang']});
      await withServer(server, () async {
        await api.get('/schemes');
        api.lang = 'hi';
        server.online = false;
        await expectLater(api.get('/schemes'), throwsA(isA<ApiException>()));
        api.lang = 'en';
        expect((await api.get('/schemes'))['lang'], 'en');
      });
    });

    test('a finished AI answer is kept and shown when the next request finds no signal', () async {
      final (api, _, _) = await _setup();
      final server = FakeServer()
        ..respond = (_) async => _json({'job_id': 'j', 'status': 'done', 'result': {'season': 'Rabi'}});
      await withServer(server, () async {
        expect((await api.cropRecommendation('p1'))['season'], 'Rabi');
        server.online = false;
        expect((await api.cropRecommendation('p1'))['season'], 'Rabi');
        expect(api.showingSaved.value, isTrue);
        await expectLater(api.cropRecommendation('other-plot'), throwsA(isA<ApiException>()));
      });
    });

    test('job status polls and the data export are never saved', () async {
      final (api, store, _) = await _setup();
      final server = FakeServer()..respond = (_) async => _json({'job_id': 'j', 'status': 'done', 'result': {}});
      await withServer(server, () async {
        await api.get('/jobs/j');
        await api.get('/me/export');
      });
      expect(store.get('GET /jobs/j?lang=en'), isNull);
      expect(store.get('GET /me/export?lang=en'), isNull);
    });

    test('a refusal that says consent is needed reaches the app', () async {
      final (api, _, _) = await _setup();
      var asked = 0;
      api.onConsentRequired = () => asked++;
      final server = FakeServer()..respond = (_) async => _json({'detail': 'Consent required: ai'}, 403);
      await withServer(server, () async {
        await expectLater(api.post('/x', {}), throwsA(isA<ApiException>().having((e) => e.status, 's', 403)));
      });
      expect(asked, 1);
    });
  });

  group('AppState: plots saved offline', () {
    test('a plot drawn with no signal is kept, shown as waiting, and sent later exactly once', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      final server = FakeServer()..online = false;
      await withServer(server, () async {
        final plot = await state.createPlot(
            name: 'North field', crop: 'wheat', country: 'IN', corners: [for (final c in _square) [c['lat']!, c['lon']!]]);
        expect(plot.pending, isTrue);
        expect(plot.id, startsWith('local-'));
        expect(plot.areaAcres, greaterThan(0));
        expect(state.plots, [plot]);
        expect(state.pendingCount, 1);

        await state.syncPending(); // still offline: nothing lost, nothing sent
        expect(state.pendingCount, 1);

        server.online = true;
        final ref = queue.items.single.id;
        server.respond = (req) async {
          if (req.method == 'POST') return _json(_serverPlot('srv-1', ref), 201);
          return _json({'plots': [_serverPlot('srv-1', ref)]});
        };
        await state.syncPending();
        expect(state.pendingCount, 0);
        expect(state.plots.map((p) => p.id), ['srv-1'], reason: 'the local copy is replaced, not duplicated');
        expect(state.plots.single.pending, isFalse);
      });
      expect(server.plotBodies.single['client_ref'], isNotEmpty, reason: 'the server can recognise a retry');
    });

    test('a plot the server refuses is reported to the farmer, not queued', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      final server = FakeServer()..respond = (_) async => _json({'detail': 'Plot is self-intersecting'}, 422);
      await withServer(server, () async {
        await expectLater(
            state.createPlot(name: 'x', crop: 'wheat', country: 'IN', corners: [for (final c in _square) [c['lat']!, c['lon']!]]),
            throwsA(isA<ApiException>().having((e) => e.status, 's', 422)));
      });
      expect(state.pendingCount, 0);
      expect(state.plots, isEmpty);
    });

    test('a saved change the server later refuses is dropped so it cannot block the rest', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      await queue.add(QueuedWrite(id: 'bad', kind: 'plot', method: 'POST', path: '/plots', body: {'name': 'x'}));
      await queue.add(QueuedWrite(id: 'good', kind: 'profile', method: 'PUT', path: '/me', body: {'language': 'hi'}));
      final server = FakeServer()
        ..respond = (req) async => req.method == 'POST' ? _json({'detail': 'bad shape'}, 422) : _json({'plots': []});
      await withServer(server, () => state.syncPending());
      expect(state.pendingCount, 0);
      expect(state.syncProblem, 'bad shape');
      expect(server.seen, contains('PUT /api/v1/me'));
    });

    test('a server error or busy signal stops the sync and keeps everything for later', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      await queue.add(QueuedWrite(id: 'a', kind: 'plot', method: 'POST', path: '/plots', body: {'name': 'x'}));
      await queue.add(QueuedWrite(id: 'b', kind: 'profile', method: 'PUT', path: '/me', body: {'language': 'hi'}));
      final server = FakeServer()..respond = (_) async => _json({'detail': 'boom'}, 503);
      await withServer(server, () => state.syncPending());
      expect(state.pendingCount, 2);
      expect(server.seen.length, 1, reason: 'no point hammering a struggling server with the rest');
      expect(state.syncProblem, isNull);
    });

    test('a write waiting for the farmer\'s consent stays queued', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      await queue.add(QueuedWrite(id: 'a', kind: 'plot', method: 'POST', path: '/plots', body: {'name': 'x'}));
      final server = FakeServer()..respond = (_) async => _json({'detail': 'Consent required: service'}, 403);
      await withServer(server, () => state.syncPending());
      expect(state.pendingCount, 1);
      expect(state.needsConsent, isFalse); // (set by the app when it is running: api.onConsentRequired)
    });

    test('a pending plot from a previous run is shown again after a restart', () async {
      final (api, _, queue) = await _setup();
      await queue.add(QueuedWrite(
          id: 'r1', kind: 'plot', method: 'POST', path: '/plots',
          body: {'name': 'Kept', 'crop': 'wheat', 'country': 'IN', 'corners': _square, 'client_ref': 'r1'}));
      final state = AppState(api, queue: queue);
      final server = FakeServer()..online = false;
      await withServer(server, () => state.loadPlots());
      expect(state.plots.single.name, 'Kept');
      expect(state.plots.single.pending, isTrue);
    });

    test('a soil sample is saved on the phone when offline, and sent to the right plot when back', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      final plot = Plot.fromJson(_serverPlot('srv-1', null));
      final server = FakeServer()..online = false;
      await withServer(server, () async {
        expect(await state.saveSoilSample(plot, {'lat': 1.0, 'lon': 2.0, 'source': 'lab_test', 'values': {'ph': 6.5}}), isFalse);
        expect(queue.items.single.path, '/plots/srv-1/soil');
        server.online = true;
        server.respond = (req) async => req.method == 'POST' ? _json({'id': 's1'}, 201) : _json({'plots': []});
        await state.syncPending();
      });
      expect(state.pendingCount, 0);
      expect(server.seen.first, 'POST /api/v1/plots/srv-1/soil');
    });

    test('a write for a plot that is itself still queued waits for that plot, then goes to its real id', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      await queue.add(QueuedWrite(
          id: 'ref-p', kind: 'plot', method: 'POST', path: '/plots',
          body: {'name': 'P', 'crop': 'wheat', 'country': 'IN', 'corners': _square, 'client_ref': 'ref-p'}));
      await queue.add(QueuedWrite(
          id: 'ref-s', kind: 'soil', method: 'POST', path: '/plots/{plot}/soil',
          body: {'lat': 1.0, 'lon': 2.0, 'values': {}}, plotRef: 'ref-p'));
      final server = FakeServer()
        ..respond = (req) async => req.url.path == '/api/v1/plots' && req.method == 'POST'
            ? _json(_serverPlot('srv-9', 'ref-p'), 201)
            : req.method == 'POST' ? _json({'id': 's'}, 201) : _json({'plots': [_serverPlot('srv-9', 'ref-p')]});
      await withServer(server, () => state.syncPending());
      expect(server.seen.take(2).toList(), ['POST /api/v1/plots', 'POST /api/v1/plots/srv-9/soil']);
      expect(state.pendingCount, 0);
    });

    test('changing language offline is remembered and sent once', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      final server = FakeServer()..online = false;
      await withServer(server, () async {
        await state.setLanguage('hi');
        await state.setLanguage('ta');
      });
      expect(queue.items.length, 1);
      expect(queue.items.single.body['language'], 'ta');
    });

    test('deleting the account also wipes what the phone remembered', () async {
      final (api, store, queue) = await _setup();
      final state = AppState(api, queue: queue);
      await store.put('k', 1);
      await queue.add(QueuedWrite(id: 'a', kind: 'profile', method: 'PUT', path: '/me', body: {}));
      final server = FakeServer()..respond = (_) async => http.Response('', 204);
      await withServer(server, () => state.deleteAccount());
      expect(store.get('k'), isNull);
      expect(queue.length, 0);
      expect(state.needsConsent, isTrue, reason: 'the fresh identity has agreed to nothing yet');
    });
  });

  group('AppState: privacy consent', () {
    test('a farmer who has not agreed is asked', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      final server = FakeServer()
        ..respond = (_) async => _json({'needs_consent': true, 'purposes': {'service': false}, 'notice_version': 'v1'});
      await withServer(server, () => state.loadConsent());
      expect(state.needsConsent, isTrue);
    });

    test('one who has agreed goes straight in', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      final server = FakeServer()
        ..respond = (_) async => _json({'needs_consent': false, 'purposes': {'service': true, 'notifications': true}});
      await withServer(server, () => state.loadConsent());
      expect(state.needsConsent, isFalse);
      expect(state.consented('notifications'), isTrue);
      expect(state.consented('ai'), isFalse);
    });

    test('with no signal the phone goes by what it remembers', () async {
      final (api, _, queue) = await _setup();
      final server = FakeServer()..online = false;
      var state = AppState(api, queue: queue);
      await withServer(server, () => state.loadConsent());
      expect(state.needsConsent, isTrue, reason: 'never agreed: still asked');

      SharedPreferences.setMockInitialValues({'consent_version': 'v1'});
      state = AppState(api, queue: queue);
      await withServer(server, () => state.loadConsent());
      expect(state.needsConsent, isFalse);
    });

    test('agreeing sends the choices, remembers the version, and turns notifications on only if allowed', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      var pushRegistered = 0;
      state.onNotificationsAllowed = () async => pushRegistered++;
      final server = FakeServer()
        ..respond = (req) async {
          final body = jsonDecode(req.body) as Map;
          final p = Map<String, dynamic>.from(body['purposes'] as Map);
          return _json({'needs_consent': false, 'purposes': p});
        };
      await withServer(server, () async {
        await state.acceptConsent('v1', {'service': true, 'ai': true, 'notifications': false});
        expect(pushRegistered, 0);
        expect(state.needsConsent, isFalse);
        await state.acceptConsent('v1', {'service': true, 'ai': true, 'notifications': true});
        expect(pushRegistered, 1);
      });
      expect((await SharedPreferences.getInstance()).getString('consent_version'), 'v1');
    });

    test('an answer to an outdated notice is refused with a 409 the screen can act on', () async {
      final (api, _, queue) = await _setup();
      final state = AppState(api, queue: queue);
      final server = FakeServer()..respond = (_) async => _json({'detail': 'The privacy notice has been updated'}, 409);
      await withServer(server, () async {
        await expectLater(state.acceptConsent('old', {'service': true}),
            throwsA(isA<ApiException>().having((e) => e.status, 's', 409)));
      });
    });
  });
}
