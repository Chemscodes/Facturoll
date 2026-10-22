import 'client.dart';

class AdminApi {
  static Future<List<dynamic>> tenantStats() async {
    final data = await ApiClient.getJson('/admin/tenants');
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> createTenant(String name) async {
    final data = await ApiClient.postJson('/admin/tenants', {'name': name});
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> createUser({
    required int tenantId,
    required String email,
    required String password,
    bool isAdmin = false,
  }) async {
    final data = await ApiClient.postJson('/admin/users', {
      'tenant_id': tenantId,
      'email': email,
      'password': password,
      'is_admin': isAdmin,
    });
    return data as Map<String, dynamic>;
  }

  static Future<List<dynamic>> userStats(
      {int? tenantId, int sinceDays = 30}) async {
    final params = <String>['since_days=$sinceDays'];
    if (tenantId != null) {
      params.add('tenant_id=$tenantId');
    }
    final path = '/admin/users/stats?${params.join('&')}';
    final data = await ApiClient.getJson(path);
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> technicalDashboard({
    int sinceHours = 24,
    int? tenantId,
    int? campaignId,
    int limitErrors = 20,
  }) async {
    final params = <String>[
      'since_hours=$sinceHours',
      'limit_errors=$limitErrors',
    ];
    if (tenantId != null) {
      params.add('tenant_id=$tenantId');
    }
    if (campaignId != null) {
      params.add('campaign_id=$campaignId');
    }
    final data =
        await ApiClient.getJson('/admin/technical/dashboard?${params.join('&')}');
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<List<dynamic>> gmailUnmatched({
    int? tenantId,
    int limit = 200,
  }) async {
    final params = <String>['limit=$limit'];
    if (tenantId != null) {
      params.add('tenant_id=$tenantId');
    }
    final data = await ApiClient.getJson('/admin/gmail-unmatched?${params.join('&')}');
    return data as List<dynamic>;
  }

  static Future<List<dynamic>> campaigns({
    int? tenantId,
    int limit = 200,
  }) async {
    final params = <String>['limit=$limit'];
    if (tenantId != null) {
      params.add('tenant_id=$tenantId');
    }
    final data = await ApiClient.getJson('/admin/campaigns?${params.join('&')}');
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> attachGmailUnmatched({
    required String attachmentKey,
    required int campaignId,
  }) async {
    final data = await ApiClient.postJson('/admin/gmail-unmatched/attach', {
      'attachment_key': attachmentKey,
      'campaign_id': campaignId,
    });
    return Map<String, dynamic>.from(data as Map);
  }
}
