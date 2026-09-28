import 'dart:convert';

import 'package:agrin/core/api.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(jsonEncode(body), status,
    headers: {'content-type': 'application/json; charset=utf-8'});

void main() {
  final api = Api(baseUrl: 'http://test', devUser: 'tester');

  test('GET is retried when the connection drops, then succeeds', () async {
    var calls = 0;
    final result = await http.runWithClient(() => api.get('/plots'), () => MockClient((req) async {
          calls++;
          if (calls == 1) throw http.ClientException('connection reset');
          return _json({'plots': []});
        }));
    expect(calls, 2);
    expect(result['plots'], isEmpty);
  });

  test('GET gives up with a readable message after repeated connection failures', () async {
    var calls = 0;
    await expectLater(
      http.runWithClient(() => api.get('/plots'), () => MockClient((req) async {
            calls++;
            throw http.ClientException('offline');
          })),
      throwsA(isA<ApiException>().having((e) => e.message, 'message', contains('No connection'))),
    );
    expect(calls, 3); // first try + 2 retries
  });

  test('a server error is NOT retried (it would add load while the server struggles)', () async {
    var calls = 0;
    await expectLater(
      http.runWithClient(() => api.get('/plots/x/crop-recommendation'), () => MockClient((req) async {
            calls++;
            return _json({'detail': 'The recommendation service could not respond. Try again.'}, 502);
          })),
      throwsA(isA<ApiException>().having((e) => e.status, 'status', 502)),
    );
    expect(calls, 1);
  });

  test('POST is never retried, so a plot cannot be created twice', () async {
    var calls = 0;
    await expectLater(
      http.runWithClient(() => api.post('/plots', {'name': 'x'}), () => MockClient((req) async {
            calls++;
            throw http.ClientException('offline');
          })),
      throwsA(isA<ApiException>()),
    );
    expect(calls, 1);
  });
}
