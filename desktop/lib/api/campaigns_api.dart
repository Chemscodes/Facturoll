import 'dart:typed_data';

import 'package:http/http.dart' as http;

import 'client.dart';

class CampaignsApi {
  static Future<List<dynamic>> list() async {
    final data = await ApiClient.getJson('/campaigns');
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> create(
      Map<String, dynamic> payload) async {
    final data = await ApiClient.postJson('/campaigns', payload);
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> update(
      int id, Map<String, dynamic> payload) async {
    final data = await ApiClient.putJson('/campaigns/$id', payload);
    return data as Map<String, dynamic>;
  }

  static Future<void> remove(int id) async {
    await ApiClient.delete('/campaigns/$id');
  }

  static Future<void> setItems(int id, List<Map<String, dynamic>> items) async {
    await ApiClient.postJson('/campaigns/$id/items', {'items': items});
  }

  static Future<List<dynamic>> items(int id) async {
    final data = await ApiClient.getJson('/campaigns/$id/items');
    return data as List<dynamic>;
  }

  static Future<List<dynamic>> setRecipients(
      int id, List<int> groupIds, List<int> supplierIds) async {
    final data = await ApiClient.putJson('/campaigns/$id/recipients', {
      'group_ids': groupIds,
      'supplier_ids': supplierIds,
    });
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> send(int id) async {
    final data = await ApiClient.postJson('/campaigns/$id/send', {});
    return data as Map<String, dynamic>;
  }

  static Future<Map<String, dynamic>> close(int id) async {
    try {
      final data = await ApiClient.postJson('/campaigns/$id/close', {});
      return data as Map<String, dynamic>;
    } catch (err) {
      // Backward-compatibility with older backend instances that do not
      // expose /campaigns/{id}/close yet (404/405).
      final message = err.toString();
      final match = RegExp(r'HTTP\s+(\d{3})').firstMatch(message);
      final statusCode =
          match == null ? null : int.tryParse(match.group(1) ?? '');
      if (statusCode != 404 && statusCode != 405) rethrow;
      final data =
          await ApiClient.putJson('/campaigns/$id', {'status': 'CLOSED'});
      return data as Map<String, dynamic>;
    }
  }

  static Future<Map<String, dynamic>> stats(int id) async {
    final data = await ApiClient.getJson('/campaigns/$id/stats');
    return data as Map<String, dynamic>;
  }

  static Future<List<dynamic>> responses(int id) async {
    final data = await ApiClient.getJson('/campaigns/$id/responses');
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> offersByProduct(
    int id, {
    bool conformOnly = false,
    bool includeMarketEstimates = false,
    bool cacheOnly = false,
    int page = 1,
    int pageSize = 20,
  }) async {
    final params = <String>[
      'page=$page',
      'page_size=$pageSize',
      if (conformOnly) 'conform_only=true',
      if (includeMarketEstimates) 'include_market_estimates=true',
      if (cacheOnly) 'cache_only=true',
    ];
    final query = params.isEmpty ? '' : '?${params.join('&')}';
    final data =
        await ApiClient.getJson('/campaigns/$id/offers/by-product$query');
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<Map<String, dynamic>> simpleProductOffers(
    int id, {
    bool cacheOnly = false,
    int page = 1,
    int pageSize = 20,
  }) async {
    final params = <String>[
      'page=$page',
      'page_size=$pageSize',
      if (cacheOnly) 'cache_only=true',
    ];
    final query = params.isEmpty ? '' : '?${params.join('&')}';
    final data =
        await ApiClient.getJson('/campaigns/$id/offers/simple-product$query');
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<Map<String, dynamic>> offerByProductDetail(
    int campaignId,
    int productId,
    int invoiceItemId,
  ) async {
    final data = await ApiClient.getJson(
      '/campaigns/$campaignId/offers/by-product/$productId/offers/$invoiceItemId',
    );
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<Map<String, dynamic>> generateMarketEstimateOffer(
    int campaignId,
    int productId,
  ) async {
    final data = await ApiClient.postJson(
      '/campaigns/$campaignId/offers/by-product/$productId/market-estimate',
      {},
      timeoutSeconds: 120,
    );
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<List<dynamic>> offersSummary(int id) async {
    final data = await ApiClient.getJson('/campaigns/$id/offers/summary');
    return data as List<dynamic>;
  }

  static Future<List<dynamic>> recipients(int id) async {
    final data = await ApiClient.getJson('/campaigns/$id/recipients');
    return data as List<dynamic>;
  }

  static Future<List<dynamic>> invoices(int id) async {
    final data = await ApiClient.getJson('/campaigns/$id/invoices');
    return data as List<dynamic>;
  }

  static Future<List<dynamic>> importedFiles({
    bool includeTechSheets = true,
  }) async {
    final query = includeTechSheets ? '?include_tech_sheets=true' : '';
    final data = await ApiClient.getJson('/campaigns/imported-files$query');
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> reassignImportedFiles({
    required int campaignId,
    List<int> invoiceIds = const <int>[],
    List<int> techSheetIds = const <int>[],
  }) async {
    final data = await ApiClient.postJson('/campaigns/imported-files/reassign', {
      'campaign_id': campaignId,
      'invoice_ids': invoiceIds,
      'tech_sheet_ids': techSheetIds,
    });
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<Map<String, dynamic>> extractImportedFiles({
    List<int> invoiceIds = const <int>[],
    List<int> techSheetIds = const <int>[],
    bool force = true,
  }) async {
    final data = await ApiClient.postJson('/campaigns/imported-files/extract', {
      'invoice_ids': invoiceIds,
      'tech_sheet_ids': techSheetIds,
      'force': force,
    }, timeoutSeconds: 300);
    return Map<String, dynamic>.from(data as Map);
  }

  static Future<Uint8List> exportCsv(int id) async {
    final data = await ApiClient.getBytes('/campaigns/$id/export/csv');
    return data;
  }

  static Future<Uint8List> exportXlsx(int id) async {
    final data = await ApiClient.getBytes('/campaigns/$id/export/xlsx');
    return data;
  }

  static Future<Uint8List> exportTopOffersXlsx(
    int id, {
    int top = 3,
    bool includeEmpty = true,
  }) async {
    final params = <String>[
      'top=$top',
      if (includeEmpty) 'include_empty=true',
    ];
    final query = params.isEmpty ? '' : '?${params.join('&')}';
    final data =
        await ApiClient.getBytes('/campaigns/$id/export/top-offers.xlsx$query');
    return data;
  }

  static Future<Uint8List> exportGptBestComplianceXlsx(int id) async {
    final data =
        await ApiClient.getBytes('/campaigns/$id/export/gpt-best-compliance.xlsx');
    return data;
  }

  static Future<Uint8List> exportGptLowestPriceXlsx(int id) async {
    final data =
        await ApiClient.getBytes('/campaigns/$id/export/gpt-lowest-price.xlsx');
    return data;
  }

  static Future<Uint8List> exportStrictTopOffersXlsx(
    int id, {
    bool includeEmpty = true,
  }) async {
    final params = <String>[
      if (includeEmpty) 'include_empty=true',
    ];
    final query = params.isEmpty ? '' : '?${params.join('&')}';
    final data = await ApiClient.getBytes(
      '/campaigns/$id/export/strict-top-offers.xlsx$query',
      timeoutSeconds: 120,
    );
    return data;
  }

  static Future<Uint8List> exportStrictTopOffersByColorXlsx(
    int id, {
    bool includeEmpty = true,
  }) async {
    final params = <String>[
      if (includeEmpty) 'include_empty=true',
    ];
    final query = params.isEmpty ? '' : '?${params.join('&')}';
    final data = await ApiClient.getBytes(
      '/campaigns/$id/export/strict-top-offers-by-color.xlsx$query',
      timeoutSeconds: 120,
    );
    return data;
  }

  static Future<Uint8List> exportStrictSelectedOffersXlsx(
    int id,
    Map<int, int> selectedOfferByProductId, {
    bool includeEmpty = true,
  }) async {
    final payload = <String, dynamic>{
      'include_empty': includeEmpty,
      'selected_offer_by_product_id': {
        for (final entry in selectedOfferByProductId.entries)
          entry.key.toString(): entry.value,
      },
    };
    final data = await ApiClient.postBytesJson(
      '/campaigns/$id/export/strict-selected-offers.xlsx',
      payload,
      timeoutSeconds: 120,
    );
    return data;
  }

  static Future<Map<String, dynamic>> purge(int id) async {
    try {
      final data = await ApiClient.postJson('/campaigns/$id/purge', {});
      return data as Map<String, dynamic>;
    } catch (err) {
      final message = err.toString();
      final match = RegExp(r'HTTP\s+(\d{3})').firstMatch(message);
      final statusCode =
          match == null ? null : int.tryParse(match.group(1) ?? '');
      if (statusCode != 404 && statusCode != 405) rethrow;
      await ApiClient.delete('/campaigns/$id');
      return {
        'status': 'ok',
        'compat_mode': 'delete_campaign_fallback',
      };
    }
  }

  static Future<Uint8List> downloadInvoiceFile(int invoiceId) async {
    final data =
        await ApiClient.getBytes('/campaigns/invoices/$invoiceId/file');
    return data;
  }

  static Future<Uint8List> downloadTechSheetFile(int techSheetId) async {
    final data =
        await ApiClient.getBytes('/campaigns/tech-sheets/$techSheetId/file');
    return data;
  }

  static Future<Uint8List> downloadCampaignAttachmentFile(
    int campaignId,
    int attachmentId,
  ) async {
    final data = await ApiClient.getBytes(
      '/campaigns/$campaignId/attachments/$attachmentId/file',
    );
    return data;
  }

  static Future<String> previewHtml(int id, {int? supplierId}) async {
    final query = supplierId == null ? '' : '?supplier_id=$supplierId';
    final data = await ApiClient.getJson('/campaigns/$id/preview$query');
    return data['html'] as String;
  }

  static Future<List<dynamic>> attachments(int id) async {
    final data = await ApiClient.getJson('/campaigns/$id/attachments');
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> uploadAttachment(
      int id, String filePath) async {
    final file = await http.MultipartFile.fromPath('file', filePath);
    final data = await ApiClient.postMultipart(
      '/campaigns/$id/attachments/upload',
      file,
      timeoutSeconds: 180,
    );
    return data as Map<String, dynamic>;
  }

  static Future<void> deleteAttachment(int id, int attachmentId) async {
    await ApiClient.delete('/campaigns/$id/attachments/$attachmentId');
  }

  static Future<List<dynamic>> extractAttachmentsOnce(
    int id, {
    bool force = false,
    List<int>? attachmentIds,
    bool asyncMode = false,
    int? expectedProductCount,
    Map<String, int>? expectedProductCounts,
    bool strictExpectedCount = true,
    String? documentTypeHint,
    Map<String, String>? documentTypeHints,
    String? layoutHint,
    Map<String, String>? layoutHints,
    String strictCountMode = 'strict',
    bool requireSpecsForAcceptance = false,
  }) async {
    final query = force ? '?force=true' : '';
    final payload = <String, dynamic>{};
    if (attachmentIds != null && attachmentIds.isNotEmpty) {
      payload['attachment_ids'] = attachmentIds;
    }
    if (asyncMode) {
      payload['async_mode'] = true;
    }
    if (expectedProductCount != null && expectedProductCount > 0) {
      payload['expected_product_count'] = expectedProductCount;
    }
    if (expectedProductCounts != null && expectedProductCounts.isNotEmpty) {
      payload['expected_product_counts'] = expectedProductCounts;
      payload['strict_expected_count'] = strictExpectedCount;
    } else if (expectedProductCount != null && expectedProductCount > 0) {
      payload['strict_expected_count'] = strictExpectedCount;
    }
    if (documentTypeHint != null && documentTypeHint.trim().isNotEmpty) {
      final hint = documentTypeHint.trim().toLowerCase();
      if (hint != 'auto') {
        payload['document_type_hint'] = hint;
      }
    }
    if (documentTypeHints != null && documentTypeHints.isNotEmpty) {
      payload['document_type_hints'] = documentTypeHints;
    }
    if (layoutHint != null && layoutHint.trim().isNotEmpty) {
      final hint = layoutHint.trim().toLowerCase();
      if (hint != 'auto') {
        payload['layout_hint'] = hint;
      }
    }
    if (layoutHints != null && layoutHints.isNotEmpty) {
      payload['layout_hints'] = layoutHints;
    }
    payload['strict_count_mode'] = strictCountMode;
    payload['require_specs_for_acceptance'] = requireSpecsForAcceptance;
    final data = await ApiClient.postJson(
      '/campaigns/$id/attachments/reextract$query',
      payload,
      timeoutSeconds: 1200,
    );
    return data as List<dynamic>;
  }

  static Future<Map<String, dynamic>> parseCharacteristicsFromText(
      String text) async {
    final data =
        await ApiClient.postJson('/campaigns/characteristics/parse-text', {
      'text': text,
    });
    return data as Map<String, dynamic>;
  }
}
