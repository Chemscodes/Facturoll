import 'client.dart';

class InvoiceExtractionsApi {
  static Future<List<dynamic>> listItems({
    int? campaignId,
    int? invoiceId,
    String? status,
    bool lowReliabilityOnly = false,
    double? lowReliabilityThreshold,
    bool productsOnly = true,
  }) async {
    final params = <String>[];
    if (campaignId != null) {
      params.add('campaign_id=$campaignId');
    }
    if (invoiceId != null) {
      params.add('invoice_id=$invoiceId');
    }
    if (status != null && status.isNotEmpty) {
      params.add('status=$status');
    }
    if (lowReliabilityOnly) {
      params.add('low_reliability_only=true');
      if (lowReliabilityThreshold != null) {
        params.add('low_reliability_threshold=$lowReliabilityThreshold');
      }
    }
    if (productsOnly) {
      params.add('products_only=true');
    }
    final query = params.isEmpty ? '' : '?${params.join('&')}';
    final data = await ApiClient.getJson('/invoices/items$query');
    return List<dynamic>.from(data as List);
  }

  static Future<Map<String, dynamic>> updateItem(
      int id, Map<String, dynamic> payload) async {
    final data = await ApiClient.putJson('/invoices/items/$id', payload);
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<List<dynamic>> extractInvoice(int invoiceId) async {
    final data = await ApiClient.postJson('/invoices/$invoiceId/extract', {});
    return List<dynamic>.from(data as List);
  }

  static Future<int> confirmSure({int? campaignId}) async {
    final query = campaignId == null ? '' : '?campaign_id=$campaignId';
    final data =
        await ApiClient.postJson('/invoices/items/confirm-sure$query', {});
    final map = Map<String, dynamic>.from(data as Map);
    return (map['updated'] as int?) ?? 0;
  }

  static Future<Map<String, dynamic>> createItem(
      Map<String, dynamic> payload) async {
    final data = await ApiClient.postJson('/invoices/items', payload);
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<int> recompute(int campaignId) async {
    final data = await ApiClient.postJson(
        '/invoices/recompute?campaign_id=$campaignId', {});
    final map = Map<String, dynamic>.from(data as Map);
    return (map['updated'] as int?) ?? 0;
  }

  static Future<Map<String, dynamic>> recomputeInvoice(int invoiceId) async {
    final data = await ApiClient.postJson(
        '/invoices/recompute-invoice?invoice_id=$invoiceId', {});
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<Map<String, dynamic>> recomputeItem(int itemId) async {
    final data =
        await ApiClient.postJson('/invoices/items/$itemId/recompute', {});
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<Map<String, dynamic>> recomputeCharacteristics(
      int campaignId) async {
    final data = await ApiClient.postJson(
        '/invoices/recompute-characteristics?campaign_id=$campaignId', {});
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<Map<String, dynamic>> rematchItem(int itemId) async {
    final data =
        await ApiClient.postJson('/invoices/items/$itemId/rematch', {});
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<List<dynamic>> relinkTechSheets({
    required int campaignId,
    int? invoiceId,
    int? invoiceItemId,
    String? senderEmail,
    double minScore = 0.60,
  }) async {
    final payload = <String, dynamic>{
      'campaign_id': campaignId,
      'min_score': minScore,
      'max_text_chars': 2000,
    };
    if (invoiceId != null) {
      payload['invoice_id'] = invoiceId;
    }
    if (invoiceItemId != null) {
      payload['invoice_item_id'] = invoiceItemId;
    }
    if (senderEmail != null && senderEmail.isNotEmpty) {
      payload['sender_email'] = senderEmail;
    }
    final data = await ApiClient.postJson('/tech-sheets/relink', payload);
    return List<dynamic>.from(data as List);
  }

  static Future<Map<String, dynamic>> getCharacteristicsMatch({
    required int itemId,
    required String requestedName,
  }) async {
    final q = Uri.encodeQueryComponent(requestedName);
    final data = await ApiClient.getJson(
      '/invoices/items/$itemId/characteristics-match?requested_name=$q',
    );
    return Map<String, dynamic>.from(data as Map);
  }
}
