import 'client.dart';

class SettingsApi {
  static Future<Map<String, dynamic>> getSettings() async {
    final data = await ApiClient.getJson('/settings');
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> updateSettings(
      Map<String, dynamic> payload) async {
    final data = await ApiClient.putJson('/settings', payload);
    return data as Map<String, dynamic>;
  }

  static Future<void> testEmail(String toEmail) async {
    await ApiClient.postJson('/settings/test-email', {'to_email': toEmail});
  }

  static Future<Map<String, dynamic>> getEmbeddingThresholds() async {
    final data = await ApiClient.getJson('/settings/embedding-thresholds');
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> updateEmbeddingThresholds(
      Map<String, dynamic> payload) async {
    final data =
        await ApiClient.putJson('/settings/embedding-thresholds', payload);
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> getCostReport(
      {int sinceDays = 30}) async {
    final data =
        await ApiClient.getJson('/settings/cost-report?since_days=$sinceDays');
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>?> getGmailAccount() async {
    final data = await ApiClient.getJson('/settings/gmail/account');
    if (data == null) return null;
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> getGmailConnectUrl() async {
    final data = await ApiClient.postJson('/settings/gmail/connect-url', {});
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> disconnectGmail() async {
    final data = await ApiClient.postJson('/settings/gmail/disconnect', {});
    return data as Map<String, dynamic>;
  }
}
