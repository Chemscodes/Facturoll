import 'client.dart';

class AuthApi {
  static Future<String> login(String email, String password) async {
    final data = await ApiClient.postJson('/auth/login', {
      'email': email,
      'password': password,
    });
    return data['access_token'] as String;
  }

  static Future<Map<String, dynamic>> me() async {
    final data = await ApiClient.getJson('/auth/me');
    return data as Map<String, dynamic>;
  }
}
