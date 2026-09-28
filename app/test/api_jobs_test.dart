import 'dart:async';
import 'dart:convert';

import 'package:agrin/core/api.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(jsonEncode(body), status,
    headers: {'content-type': 'application/json; charset=utf-8'});

void main() {
  Api makeApi() => Api(baseUrl: 'http://test', devUser: 'tester')..pollDelay = (_) => Duration.zero;

  test('a ready answer is returned straight away with no polling', () async {
    final paths = <String>[];
    final result = await http.runWithClient(() => makeApi().cropRecommendation('p1'), () => MockClient((req) async {
          paths.add('${req.method} ${req.url.path}');
          return _json({'job_id': 'j1', 'status': 'done', 'result': {'plot_id': 'p1', 'season': 'Rabi'}});
        }));
    expect(result['season'], 'Rabi');
    expect(paths, ['POST /api/v1/plots/p1/crop-recommendation/jobs']);
  });

  test('an accepted job is polled until it is done', () async {
    var polls = 0;
    final result = await http.runWithClient(() => makeApi().cropRecommendation('p1'), () => MockClient((req) async {
          if (req.method == 'POST') return _json({'job_id': 'j1', 'status': 'queued', 'retry_after_s': 2}, 202);
          expect(req.url.path, '/api/v1/jobs/j1');
          polls++;
          return polls < 3
              ? _json({'job_id': 'j1', 'status': 'running', 'retry_after_s': 2})
              : _json({'job_id': 'j1', 'status': 'done', 'result': {'season': 'Rabi'}});
        }));
    expect(result['season'], 'Rabi');
    expect(polls, 3);
  });

  test('a failed job becomes an ApiException with the same status and message as the direct endpoint', () async {
    await expectLater(
      http.runWithClient(() => makeApi().cropRecommendation('p1'), () => MockClient((req) async {
            if (req.method == 'POST') return _json({'job_id': 'j1', 'status': 'running'}, 202);
            return _json({
              'job_id': 'j1',
              'status': 'failed',
              'error': {'status': 502, 'message': 'The recommendation service could not respond. Try again.'}
            });
          })),
      throwsA(isA<ApiException>()
          .having((e) => e.status, 'status', 502)
          .having((e) => e.message, 'message', contains('Try again'))),
    );
  });

  test('an immediate error from the start request is surfaced as-is', () async {
    await expectLater(
      http.runWithClient(() => makeApi().cropRecommendation('p1'),
          () => MockClient((req) async => _json({'detail': 'Set GEMINI_API_KEY'}, 503))),
      throwsA(isA<ApiException>().having((e) => e.status, 'status', 503)),
    );
  });

  test('a plot that does not exist is a real 404, not a reason to fall back', () async {
    var calls = 0;
    await expectLater(
      http.runWithClient(() => makeApi().cropRecommendation('nope'), () => MockClient((req) async {
            calls++;
            return _json({'detail': 'Plot not found'}, 404);
          })),
      throwsA(isA<ApiException>().having((e) => e.message, 'message', 'Plot not found')),
    );
    expect(calls, 1);
  });

  test('an older server without job endpoints is served by the direct endpoint', () async {
    final paths = <String>[];
    final result = await http.runWithClient(() => makeApi().cropRecommendation('p1'), () => MockClient((req) async {
          paths.add('${req.method} ${req.url.path}');
          if (req.url.path.endsWith('/jobs')) return _json({'detail': 'Not Found'}, 404);
          return _json({'plot_id': 'p1', 'season': 'Rabi'});
        }));
    expect(result['season'], 'Rabi');
    expect(paths, ['POST /api/v1/plots/p1/crop-recommendation/jobs', 'GET /api/v1/plots/p1/crop-recommendation']);
  });

  test('photo diagnosis also runs as a job and falls back on an older server', () async {
    final bytes = utf8.encode('fake-jpeg');
    final paths = <String>[];
    final result = await http.runWithClient(
        () => makeApi().diagnose(bytes as dynamic, 'leaf.jpg', plotId: 'p1'), () => MockClient((req) async {
              paths.add('${req.method} ${req.url.path}');
              if (req.url.path.endsWith('/diagnosis/jobs')) return _json({'detail': 'Not Found'}, 404);
              return _json({'status': 'healthy'});
            }));
    expect(result['status'], 'healthy');
    expect(paths, ['POST /api/v1/diagnosis/jobs', 'POST /api/v1/diagnosis']);
  });

  test('resilience, water, market and forecast are jobs too, with the query passed along', () async {
    final seen = <String>[];
    final api = makeApi();
    await http.runWithClient(() async {
      await api.resilience(plotId: 'p1');
      await api.waterTips();
      await api.market('p1');
      await api.forecast('p1', days: 5);
    }, () => MockClient((req) async {
          seen.add('${req.method} ${req.url.path}${req.url.hasQuery ? '?${req.url.queryParameters.entries.where((e) => e.key != 'lang').map((e) => '${e.key}=${e.value}').join('&')}' : ''}');
          return _json({'job_id': 'j', 'status': 'done', 'result': {}});
        }));
    expect(seen, [
      'POST /api/v1/resilience/jobs?plot_id=p1',
      'POST /api/v1/water-tips/jobs?',
      'POST /api/v1/plots/p1/market/jobs?',
      'POST /api/v1/plots/p1/forecast/jobs?days=5',
    ]);
  });

  test('those screens fall back to their direct endpoint on an older server', () async {
    final paths = <String>[];
    final result = await http.runWithClient(() => makeApi().market('p1'), () => MockClient((req) async {
          paths.add('${req.method} ${req.url.path}');
          if (req.url.path.endsWith('/jobs')) return _json({'detail': 'Not Found'}, 404);
          return _json({'principles': []});
        }));
    expect(result['principles'], isEmpty);
    expect(paths, ['POST /api/v1/plots/p1/market/jobs', 'GET /api/v1/plots/p1/market']);
  });

  test('going to the background asks for a push for the job still being waited on, and only that one', () async {
    final api = makeApi();
    final gate = Completer<void>();
    final notified = <String>[];
    await http.runWithClient(() async {
      final pending = api.cropRecommendation('p1');
      await Future<void>.delayed(const Duration(milliseconds: 50)); // it is now polling job j1
      await api.notifyPendingJobs();
      gate.complete();
      expect((await pending)['season'], 'Rabi');
      await api.notifyPendingJobs(); // finished: nothing left to ask for
    }, () => MockClient((req) async {
          if (req.url.path == '/api/v1/plots/p1/crop-recommendation/jobs') return _json({'job_id': 'j1', 'status': 'queued'}, 202);
          if (req.url.path.endsWith('/notify')) {
            notified.add(req.url.path);
            return _json({'job_id': 'j1', 'status': 'will_notify'});
          }
          await gate.future;
          return _json({'job_id': 'j1', 'status': 'done', 'result': {'season': 'Rabi'}});
        }));
    expect(notified, ['/api/v1/jobs/j1/notify']);
  });
}
