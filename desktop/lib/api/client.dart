import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;

import '../config/app_backend_config.dart';

class ApiClient {
  static const String localBackendUrl = AppBackendConfig.localBackendUrl;
  static const String vpsBackendUrl = AppBackendConfig.vpsBackendUrl;
  static const String _defaultBaseUrl = AppBackendConfig.defaultBaseUrl;
  static const _tokenKey = 'jwt_token';
  static const _baseUrlKey = 'api_base_url';
  static const _storage = FlutterSecureStorage();
  static final http.Client _client = http.Client();
  static String _baseUrl = _defaultBaseUrl;
  static String? _token;
  static Future<void> Function()? onUnauthorized;

  static String? get token => _token;
  static String get baseUrl => _baseUrl;
  static String get _effectiveBaseUrl => _baseUrl;

  static Future<void> loadToken() async {
    _token = await _storage.read(key: _tokenKey);
  }

  static Future<void> loadBaseUrl() async {
    if (AppBackendConfig.forceLocalBackend) {
      _baseUrl = localBackendUrl;
      await _storage.write(key: _baseUrlKey, value: _baseUrl);
      return;
    }
    final stored = await _storage.read(key: _baseUrlKey);
    final fallback = _normalizeBaseUrl(vpsBackendUrl);
    final normalizedStored =
        stored == null || stored.trim().isEmpty ? '' : _normalizeBaseUrl(stored);
    final fallbackUri = Uri.tryParse(fallback);
    final storedUri = normalizedStored.isEmpty ? null : Uri.tryParse(normalizedStored);

    // When not forcing local backend, never keep localhost from old sessions.
    if (normalizedStored.isEmpty ||
        normalizedStored.contains('127.0.0.1') ||
        normalizedStored.contains('localhost')) {
      _baseUrl = fallback;
    } else if (fallbackUri != null &&
        storedUri != null &&
        fallbackUri.host == storedUri.host &&
        fallbackUri.scheme != storedUri.scheme) {
      // Keep configured scheme for same host (e.g. force http when TLS is broken).
      _baseUrl = fallback;
    } else {
      _baseUrl = normalizedStored;
    }
    await _storage.write(key: _baseUrlKey, value: _baseUrl);
  }

  static Future<void> load() async {
    await Future.wait([loadToken(), loadBaseUrl()]);
  }

  static Future<void> setToken(String? token) async {
    _token = token;
    if (token == null) {
      await _storage.delete(key: _tokenKey);
    } else {
      await _storage.write(key: _tokenKey, value: token);
    }
  }

  static Future<void> setBaseUrl(String url) async {
    final normalized = _normalizeBaseUrl(url);
    _baseUrl = normalized;
    await _storage.write(key: _baseUrlKey, value: _baseUrl);
  }

  static Future<void> useLocalBackend() async {
    await setBaseUrl(localBackendUrl);
  }

  static Future<void> useVpsBackend() async {
    await setBaseUrl(vpsBackendUrl);
  }

  static String _normalizeBaseUrl(String url) {
    var value = url.trim();
    if (value.endsWith('/')) {
      value = value.substring(0, value.length - 1);
    }
    if (!value.startsWith('http://') && !value.startsWith('https://')) {
      value = 'http://$value';
    }
    return value;
  }

  static Map<String, String> _headers({bool json = true}) {
    final headers = <String, String>{};
    if (json) {
      headers['Content-Type'] = 'application/json';
    }
    if (_token != null) {
      headers['Authorization'] = 'Bearer $_token';
    }
    return headers;
  }

  static void _check(http.Response response) {
    if (response.statusCode == 401 && _token != null) {
      unawaited(setToken(null));
      final handler = onUnauthorized;
      if (handler != null) {
        unawaited(handler());
      }
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      var message = response.body;
      if (message.isEmpty) {
        message = 'Request failed';
      }
      try {
        final data = jsonDecode(response.body);
        if (data is Map) {
          if (data['detail'] != null) {
            message = data['detail'].toString();
          } else if (data['message'] != null) {
            message = data['message'].toString();
          }
        }
      } catch (_) {}
      throw Exception('HTTP ${response.statusCode}: $message');
    }
  }

  static String formatError(Object err) {
    final message = err.toString();
    if (message.contains('TimeoutException')) {
      return 'Timeout extraction: traitement OCR trop long. Reessaie, ou reduis le nombre/poids des PDFs.';
    }
    if (message.contains('SocketException') ||
        message.contains('Connection refused') ||
        message.contains('Failed host lookup') ||
        message.contains('Connection reset by peer') ||
        message.contains('Connection timed out') ||
        message.contains('HandshakeException')) {
      return 'Cannot reach backend at $_baseUrl';
    }
    return message.replaceFirst('Exception: ', '');
  }

  static bool _isTransientNetworkError(Object err) {
    if (err is SocketException || err is TimeoutException) {
      return true;
    }
    if (err is http.ClientException) {
      return true;
    }
    final message = err.toString();
    return message.contains('Connection reset by peer') ||
        message.contains('Connection timed out') ||
        message.contains('Failed host lookup');
  }

  static Future<T> _withNetworkRetry<T>(
    Future<T> Function() request, {
    int attempts = 3,
  }) async {
    Object? lastError;
    for (var i = 0; i < attempts; i++) {
      try {
        return await request();
      } catch (err) {
        lastError = err;
        final shouldRetry = i < attempts - 1 && _isTransientNetworkError(err);
        if (!shouldRetry) {
          rethrow;
        }
        await Future.delayed(Duration(milliseconds: 350 * (i + 1)));
      }
    }
    throw lastError ?? Exception('Network request failed');
  }

  static Future<dynamic> getJson(String path) async {
    final response = await _withNetworkRetry(() => _client
        .get(Uri.parse('$_effectiveBaseUrl$path'), headers: _headers())
        .timeout(const Duration(seconds: 60)));
    _check(response);
    return jsonDecode(response.body);
  }

  static Future<dynamic> postJson(
    String path,
    Map<String, dynamic> body, {
    int timeoutSeconds = 25,
  }) async {
    final response = await _client
        .post(
          Uri.parse('$_effectiveBaseUrl$path'),
          headers: _headers(),
          body: jsonEncode(body),
        )
        .timeout(Duration(seconds: timeoutSeconds));
    _check(response);
    return response.body.isEmpty ? null : jsonDecode(response.body);
  }

  static Future<dynamic> putJson(String path, Map<String, dynamic> body) async {
    final response = await _client
        .put(
          Uri.parse('$_effectiveBaseUrl$path'),
          headers: _headers(),
          body: jsonEncode(body),
        )
        .timeout(const Duration(seconds: 25));
    _check(response);
    return response.body.isEmpty ? null : jsonDecode(response.body);
  }

  static Future<void> delete(String path) async {
    final response = await _client
        .delete(Uri.parse('$_effectiveBaseUrl$path'), headers: _headers())
        .timeout(const Duration(seconds: 25));
    _check(response);
  }

  static Future<dynamic> postForm(String path, Map<String, String> form) async {
    final response = await _client
        .post(Uri.parse('$_effectiveBaseUrl$path'), body: form)
        .timeout(const Duration(seconds: 25));
    _check(response);
    return response.body;
  }

  static Future<dynamic> postMultipart(
    String path,
    http.MultipartFile file, {
    int timeoutSeconds = 120,
  }) async {
    final request =
        http.MultipartRequest('POST', Uri.parse('$_effectiveBaseUrl$path'));
    request.headers.addAll(_headers(json: false));
    request.files.add(file);
    final timeout = Duration(seconds: timeoutSeconds);
    final streamed = await request.send().timeout(timeout);
    final response = await http.Response.fromStream(streamed).timeout(timeout);
    _check(response);
    return response.body.isEmpty ? null : jsonDecode(response.body);
  }

  static Future<Uint8List> getBytes(
    String path, {
    int timeoutSeconds = 25,
  }) async {
    final response = await _withNetworkRetry(() => _client
        .get(Uri.parse('$_effectiveBaseUrl$path'),
            headers: _headers(json: false))
        .timeout(Duration(seconds: timeoutSeconds)));
    _check(response);
    return response.bodyBytes;
  }

  static Future<Uint8List> postBytesJson(
    String path,
    Map<String, dynamic> body, {
    int timeoutSeconds = 60,
  }) async {
    final response = await _client
        .post(
          Uri.parse('$_effectiveBaseUrl$path'),
          headers: _headers(),
          body: jsonEncode(body),
        )
        .timeout(Duration(seconds: timeoutSeconds));
    _check(response);
    return response.bodyBytes;
  }
}
