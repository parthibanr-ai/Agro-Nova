import 'dart:convert';
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

  Future<dynamic> get(String path, {Map<String, String>? query}) async =>
      _decode(await http.get(_uri(path, query), headers: await _headers()));

  Future<dynamic> post(String path, Map<String, dynamic> body) async =>
      _decode(await http.post(_uri(path), headers: await _headers(), body: jsonEncode(body)));

  Future<dynamic> put(String path, Map<String, dynamic> body) async =>
      _decode(await http.put(_uri(path), headers: await _headers(), body: jsonEncode(body)));

  Future<dynamic> delete(String path) async =>
      _decode(await http.delete(_uri(path), headers: await _headers()));

  Future<dynamic> diagnose(Uint8List bytes, String filename, {String? crop, String? plotId, String? notes}) async {
    final req = http.MultipartRequest('POST', _uri('/diagnosis'))
      ..headers.addAll(await _headers(json: false))
      ..files.add(http.MultipartFile.fromBytes('image', bytes,
          filename: filename,
          contentType: MediaType('image', filename.toLowerCase().endsWith('png') ? 'png' : 'jpeg')));
    if (crop != null) req.fields['crop'] = crop;
    if (plotId != null) req.fields['plot_id'] = plotId;
    if (notes != null) req.fields['notes'] = notes;
    return _decode(await http.Response.fromStream(await req.send()));
  }
}
