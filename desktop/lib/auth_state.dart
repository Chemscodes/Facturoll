import 'package:flutter/material.dart';

import 'api/auth_api.dart';
import 'api/client.dart';

class AuthState extends ChangeNotifier {
  bool _loading = true;
  String? _error;
  bool _loggingOut = false;
  bool _isAdmin = false;
  String? _email;

  AuthState() {
    ApiClient.onUnauthorized = _handleUnauthorized;
  }

  bool get isLoading => _loading;
  bool get isLoggedIn => ApiClient.token != null;
  bool get isAdmin => _isAdmin;
  String? get email => _email;
  String? get error => _error;

  Future<void> load() async {
    await ApiClient.load();
    if (ApiClient.token != null) {
      await _loadProfile();
    }
    _loading = false;
    notifyListeners();
  }

  Future<void> login(String email, String password) async {
    _error = null;
    notifyListeners();
    try {
      final token = await AuthApi.login(email, password);
      await ApiClient.setToken(token);
      await _loadProfile();
    } catch (err) {
      _error = ApiClient.formatError(err);
      _isAdmin = false;
      _email = null;
      await ApiClient.setToken(null);
    }
    notifyListeners();
  }

  Future<void> logout() async {
    _error = null;
    _isAdmin = false;
    _email = null;
    await ApiClient.setToken(null);
    notifyListeners();
  }

  Future<void> _handleUnauthorized() async {
    if (_loggingOut) {
      return;
    }
    _loggingOut = true;
    try {
      _error = 'Session expiree. Merci de vous reconnecter.';
      _isAdmin = false;
      _email = null;
      await ApiClient.setToken(null);
      notifyListeners();
    } finally {
      _loggingOut = false;
    }
  }

  Future<void> _loadProfile() async {
    try {
      final profile = await AuthApi.me();
      _email = profile['email'] as String?;
      _isAdmin = profile['is_admin'] == true;
    } catch (err) {
      _error = ApiClient.formatError(err);
      _email = null;
      _isAdmin = false;
    }
  }
}
