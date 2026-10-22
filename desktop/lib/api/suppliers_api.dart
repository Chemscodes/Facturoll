import 'package:http/http.dart' as http;

import 'client.dart';

class SuppliersApi {
  static Future<List<dynamic>> list() async {
    final data = await ApiClient.getJson('/suppliers');
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> create(Map<String, dynamic> payload) async {
    final data = await ApiClient.postJson('/suppliers', payload);
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> update(int id, Map<String, dynamic> payload) async {
    final data = await ApiClient.putJson('/suppliers/$id', payload);
    return data as Map<String, dynamic>;
  }

  static Future<void> remove(int id) async {
    await ApiClient.delete('/suppliers/$id');
  }

  static Future<Map<String, dynamic>> importCsv(String path) async {
    final file = await http.MultipartFile.fromPath('file', path);
    final data = await ApiClient.postMultipart('/bulk/suppliers', file);
    return data as Map<String, dynamic>;
  }
}
