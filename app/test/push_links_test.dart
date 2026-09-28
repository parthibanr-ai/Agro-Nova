import 'dart:convert';

import 'package:agrin/core/api.dart';
import 'package:agrin/core/app_state.dart';
import 'package:agrin/core/push_links.dart';
import 'package:agrin/screens/crop_recommendation_screen.dart';
import 'package:agrin/screens/diagnosis_screen.dart';
import 'package:agrin/screens/forecast_screen.dart';
import 'package:agrin/screens/info_screens.dart';
import 'package:agrin/screens/push_router.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(jsonEncode(body), status,
    headers: {'content-type': 'application/json; charset=utf-8'});

Plot _plot(String id) => Plot.fromJson({
      'id': id,
      'name': 'P',
      'crop': 'wheat',
      'country': 'IN',
      'area_acres': 1.0,
      'centroid': {'lat': 1.0, 'lon': 2.0},
      'corners': [
        {'lat': 1.0, 'lon': 2.0}
      ],
    });

void main() {
  group('PushLink', () {
    test('reads the data the server puts in a "ready" notification', () {
      final l = PushLink.fromData({'job_id': 'j1', 'kind': 'crop_recommendation', 'plot_id': 'p1'})!;
      expect((l.jobId, l.kind, l.plotId), ('j1', 'crop_recommendation', 'p1'));
      expect(PushLink.fromData({'job_id': 'j2', 'kind': 'resilience'})!.plotId, isNull);
    });

    test('other notifications (digests, alerts) are not links', () {
      expect(PushLink.fromData({}), isNull);
      expect(PushLink.fromData({'kind': 'scheme'}), isNull);
      expect(PushLink.fromData({'job_id': '', 'kind': 'forecast'}), isNull);
      expect(PushLink.fromData({'job_id': 5, 'kind': 'forecast'}), isNull);
    });
  });

  group('screenForPush', () {
    final plots = [_plot('p1')];
    PushLink link(String kind, [String? plot = 'p1']) => PushLink(kind: kind, jobId: 'j', plotId: plot);

    test('each kind of answer opens its own screen, showing that job', () {
      expect(screenForPush(link('crop_recommendation'), plots), isA<CropRecommendationScreen>());
      expect(screenForPush(link('forecast'), plots), isA<ForecastScreen>());
      expect(screenForPush(link('market'), plots), isA<MarketScreen>());
      expect(screenForPush(link('diagnosis'), plots), isA<DiagnosisScreen>());
      expect(screenForPush(link('resilience'), plots), isA<ResilienceScreen>());
      expect(screenForPush(link('water_tips'), plots), isA<WaterTipsScreen>());
      expect((screenForPush(link('crop_recommendation'), plots) as CropRecommendationScreen).openJobId, 'j');
    });

    test('screens that need a plot are not opened for a plot that no longer exists', () {
      expect(screenForPush(link('crop_recommendation', 'gone'), plots), isNull);
      expect(screenForPush(link('forecast', null), plots), isNull);
      expect(screenForPush(link('resilience', null), plots), isA<ResilienceScreen>(), reason: 'works without a plot');
    });

    test('an unknown kind opens nothing (the app just opens)', () {
      expect(screenForPush(link('something_new'), plots), isNull);
    });
  });

  group('resultOrFresh', () {
    Api api() => Api(baseUrl: 'http://test', devUser: 't')..connectRetries = 0;

    test('with no job it just asks afresh', () async {
      expect(await resultOrFresh(api(), null, () async => 'fresh'), 'fresh');
    });

    test('shows the finished job when the server still has it', () async {
      final r = await http.runWithClient(
          () => resultOrFresh(api(), 'j1', () async => 'fresh'),
          () => MockClient((req) async {
                expect(req.url.path, '/api/v1/jobs/j1');
                return _json({'job_id': 'j1', 'status': 'done', 'result': {'season': 'Rabi'}});
              }));
      expect(r['season'], 'Rabi');
    });

    test('asks afresh when the server has forgotten the job (a notification tapped much later)', () async {
      final r = await http.runWithClient(() => resultOrFresh(api(), 'old', () async => 'fresh'),
          () => MockClient((req) async => _json({'detail': 'Job not found'}, 404)));
      expect(r, 'fresh');
    });

    test('any other failure is shown, not hidden', () async {
      await expectLater(
        http.runWithClient(() => resultOrFresh(api(), 'j1', () async => 'fresh'),
            () => MockClient((req) async => _json({'detail': 'boom'}, 500))),
        throwsA(isA<ApiException>().having((e) => e.status, 's', 500)),
      );
    });

    test('a job that failed shows its error', () async {
      await expectLater(
        http.runWithClient(() => resultOrFresh(api(), 'j1', () async => 'fresh'),
            () => MockClient((req) async => _json({'job_id': 'j1', 'status': 'failed', 'error': {'status': 502, 'message': 'try again'}}))),
        throwsA(isA<ApiException>().having((e) => e.status, 's', 502)),
      );
    });
  });
}
