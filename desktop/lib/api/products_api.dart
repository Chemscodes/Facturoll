import 'package:http/http.dart' as http;

import 'client.dart';

class ProductsApi {
  static Future<List<dynamic>> list() async {
    final data = await ApiClient.getJson('/products');
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> create(Map<String, dynamic> payload) async {
    final data = await ApiClient.postJson('/products', payload);
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> update(int id, Map<String, dynamic> payload) async {
    final data = await ApiClient.putJson('/products/$id', payload);
    return data as Map<String, dynamic>;
  }

  static Future<void> remove(int id) async {
    await ApiClient.delete('/products/$id');
  }

  static Future<Map<String, dynamic>> importCsv(String path) async {
    final file = await http.MultipartFile.fromPath('file', path);
    final data = await ApiClient.postMultipart('/bulk/products', file);
    return data as Map<String, dynamic>;
  }
}
