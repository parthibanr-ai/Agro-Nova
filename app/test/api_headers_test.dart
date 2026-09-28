import 'dart:convert';

import 'package:agrin/core/api.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(jsonEncode(body), status,
    headers: {'content-type': 'application/json; charset=utf-8'});

void main() {
  test('the App Check token and the ID token are sent on every request', () async {
    final api = Api(
        baseUrl: 'http://test',
        tokenProvider: () async => 'id-token',
        appCheckTokenProvider: () async => 'app-check-token');
    late Map<String, String> headers;
    await http.runWithClient(() => api.get('/me'), () => MockClient((req) async {
          headers = req.headers;
          return _json({});
        }));
    expect(headers['Authorization'], 'Bearer id-token');
    expect(headers['X-Firebase-AppCheck'], 'app-check-token');
  });

  test('no App Check header without a provider, and a failing provider does not break the request', () async {
    final seen = <Map<String, String>>[];
    MockClient client() => MockClient((req) async {
          seen.add(req.headers);
          return _json({});
        });
    await http.runWithClient(() => Api(baseUrl: 'http://test', devUser: 'x').get('/me'), client);
    await http.runWithClient(
        () => Api(baseUrl: 'http://test', devUser: 'x', appCheckTokenProvider: () async => throw StateError('no attestation'))
            .get('/me'),
        client);
    expect(seen, hasLength(2));
    for (final h in seen) {
      expect(h.containsKey('X-Firebase-AppCheck'), isFalse);
    }
  });

  test('deleting the account sends the confirmation and uses DELETE', () async {
    late http.Request request;
    await http.runWithClient(() => Api(baseUrl: 'http://test', devUser: 'x').delete('/me', query: {'confirm': 'true'}),
        () => MockClient((req) async {
              request = req;
              return http.Response('', 204);
            }));
    expect(request.method, 'DELETE');
    expect(request.url.path, '/api/v1/me');
    expect(request.url.queryParameters['confirm'], 'true');
  });
}
