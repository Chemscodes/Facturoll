import 'client.dart';

class TemplatesApi {
  static Future<List<dynamic>> list() async {
    final data = await ApiClient.getJson('/templates');
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> create(Map<String, dynamic> payload) async {
    final data = await ApiClient.postJson('/templates', payload);
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> update(int id, Map<String, dynamic> payload) async {
    final data = await ApiClient.putJson('/templates/$id', payload);
    return data as Map<String, dynamic>;
  }

  static Future<void> remove(int id) async {
    await ApiClient.delete('/templates/$id');
  }
}
