import 'client.dart';

class GroupsApi {
  static Future<List<dynamic>> list() async {
    final data = await ApiClient.getJson('/groups');
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> create(Map<String, dynamic> payload) async {
    final data = await ApiClient.postJson('/groups', payload);
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> update(int id, Map<String, dynamic> payload) async {
    final data = await ApiClient.putJson('/groups/$id', payload);
    return data as Map<String, dynamic>;
  }

  static Future<void> remove(int id) async {
    await ApiClient.delete('/groups/$id');
  }

  static Future<List<dynamic>> listMembers(int groupId) async {
    final data = await ApiClient.getJson('/groups/$groupId/members');
    return data as List<dynamic>;
  }

  static Future<List<dynamic>> replaceMembers(int groupId, List<int> supplierIds) async {
    final data = await ApiClient.postJson('/groups/$groupId/members', {
      'supplier_ids': supplierIds,
    });
    return data as List<dynamic>;
  }
}
