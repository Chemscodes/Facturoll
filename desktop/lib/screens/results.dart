import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../api/client.dart';
import '../api/campaigns_api.dart';
import '../api/invoice_extractions_api.dart';
import '../api/settings_api.dart';
import '../api/suppliers_api.dart';
import '../config/app_thresholds.dart';
import '../utils/file_opening.dart';
import '../widgets/empty_state.dart';

class ResultsScreen extends StatefulWidget {
  const ResultsScreen({super.key});

  @override
  State<ResultsScreen> createState() => _ResultsScreenState();
}

class _ResultsScreenState extends State<ResultsScreen> {
  static const double _charThresholdMin =
      AppThresholds.fixedCharacteristicsThreshold;
  static const double _charThresholdFixed =
      AppThresholds.fixedCharacteristicsThreshold;
  static const double _resultsMinGptMatch = 0.50;
  static const int _offersPageSize = 20;
  static const int _initialOffersVisible = 5;
  static const String _uiStateStorageKey = 'results_ui_state_v1';
  static const _uiStateStorage = FlutterSecureStorage();
  bool _loading = true;
  bool _savingThresholds = false;
  bool _thresholdRecomputeRunning = false;
  bool _thresholdRecomputeNeedsFull = false;
  bool _thresholdRecomputeNeedsCharOnly = false;
  String? _error;
  List<dynamic> _campaigns = [];
  int? _selectedCampaignId;
  int? _lastSelectedCampaignId;
  int? _resumeCampaignId;
  Map<String, dynamic>? _stats;
  List<dynamic> _conformOffers = [];
  int _offersCurrentPage = 1;
  int _offersTotal = 0;
  bool _offersHasMore = false;
  bool _offersLoadingMore = false;
  final Map<int, int> _visibleOffersByProduct = {};
  final Map<int, Map<int, int>> _visibleOffersByCampaign = {};
  int? _loadedOffersCampaignId;
  final Map<int, int> _activeTabByCampaign = {};
  final Set<int> _loadingTechDetailOfferIds = <int>{};
  final Map<int, String> _productQueryByCampaign = {};
  final Map<int, double> _productScrollOffsetByCampaign = {};
  final Map<int, Map<String, dynamic>> _futureFiltersByCampaign = {};
  List<dynamic> _recipients = [];
  List<dynamic> _responses = [];
  Map<int, String> _supplierNames = {};
  String _productQuery = '';
  final _productSearchController = TextEditingController();
  final ScrollController _productListController = ScrollController();
  double _embedConfirm = AppThresholds.defaultConfirmThreshold;
  double _embedVerify = AppThresholds.fixedVerifyThreshold;
  double _charMatchThreshold = AppThresholds.fixedCharacteristicsThreshold;
  double _savedEmbedConfirm = AppThresholds.defaultConfirmThreshold;
  double _savedCharMatchThreshold = AppThresholds.fixedCharacteristicsThreshold;
  Timer? _thresholdAutoApplyTimer;
  Timer? _uiStatePersistTimer;
  bool _showBackToTop = false;

  void _showError(Object err) {
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
  }

  Future<bool> _openWithShell(String path) async {
    return openWithShell(path);
  }

  Future<void> _openLocalFile(String? path) async {
    final value = (path ?? '').trim();
    if (value.isEmpty) {
      return;
    }
    final opened = await _openWithShell(value);
    if (!opened) {
      if (!mounted) return;
      _showError(Exception('Impossible d\'ouvrir le fichier'));
    }
  }

  Future<bool> _tryOpenLocalFile(String path) async {
    return _openWithShell(path);
  }

  bool _isLocalWindowsPath(String value) {
    return isLocalWindowsPath(value);
  }

  String _ensureFileExtension(String path, String extension) {
    final ext = extension.startsWith('.') ? extension : '.$extension';
    if (path.toLowerCase().endsWith(ext.toLowerCase())) {
      return path;
    }
    return '$path$ext';
  }

  Map<String, dynamic>? _selectedCampaignMap() {
    final id = _selectedCampaignId;
    if (id == null) return null;
    for (final raw in _campaigns) {
      if (raw is Map && raw['id'] == id) {
        return Map<String, dynamic>.from(raw);
      }
    }
    return null;
  }

  String _formatCampaignDate(dynamic raw) {
    final value = DateTime.tryParse((raw ?? '').toString());
    if (value == null) return '-';
    final local = value.toLocal();
    final month = local.month.toString().padLeft(2, '0');
    final day = local.day.toString().padLeft(2, '0');
    return '${local.year}-$month-$day';
  }

  String _campaignCountdown(dynamic raw) {
    final value = DateTime.tryParse((raw ?? '').toString());
    if (value == null) return 'Date depot non renseignee';
    final now = DateTime.now();
    final target =
        DateTime(value.year, value.month, value.day, 23, 59, 59).toLocal();
    final diff = target.difference(now);
    final days = diff.inDays;
    if (diff.isNegative) return 'Depot depasse de ${days.abs()} jour(s)';
    return 'Depot dans ${days}j ${diff.inHours.remainder(24)}h';
  }

  ({Color background, Color border, Color text}) _campaignCountdownColors(
    dynamic raw,
  ) {
    final value = DateTime.tryParse((raw ?? '').toString());
    if (value == null) {
      return (
        background: const Color(0xFFF2F4F7),
        border: const Color(0xFFD0D5DD),
        text: const Color(0xFF344054),
      );
    }
    final now = DateTime.now();
    final target = DateTime(value.year, value.month, value.day, 23, 59, 59).toLocal();
    final diff = target.difference(now);
    if (diff.isNegative) {
      return (
        background: const Color(0xFFFDECEC),
        border: const Color(0xFFF5C2C7),
        text: const Color(0xFFB42318),
      );
    }
    if (diff.inDays <= 3) {
      return (
        background: const Color(0xFFFFE8CC),
        border: const Color(0xFFF7B267),
        text: const Color(0xFFB54708),
      );
    }
    return (
      background: const Color(0xFFF2F4F7),
      border: const Color(0xFFD0D5DD),
      text: const Color(0xFF344054),
    );
  }

  Widget _campaignMetaBanner() {
    final campaign = _selectedCampaignMap();
    if (campaign == null) return const SizedBox.shrink();
    final countdownColors = _campaignCountdownColors(campaign['deposit_date']);
    final processing = [
      if (campaign['processing_in_progress'] == true) 'En cours',
      if (campaign['processing_validated'] == true) 'Valide',
    ].join(' / ');
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                _metaBadge('Etablissement',
                    (campaign['establishment_name'] ?? '-').toString()),
                _metaBadge('Lot', (campaign['lot_reference'] ?? '-').toString()),
                _metaBadge(
                    'Type', (campaign['establishment_type'] ?? '-').toString()),
                _metaBadge('Depot', _formatCampaignDate(campaign['deposit_date'])),
                _metaBadge(
                  'Compte a rebours',
                  _campaignCountdown(campaign['deposit_date']),
                  backgroundColor: countdownColors.background,
                  borderColor: countdownColors.border,
                  textColor: countdownColors.text,
                ),
                _metaBadge('Traitement', processing.isEmpty ? '-' : processing),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              'Adresse: ${(campaign['establishment_address'] ?? '-').toString()}',
              style: const TextStyle(fontSize: 12, color: Color(0xFF475467)),
            ),
            const SizedBox(height: 4),
            Text(
              'Telephone: ${(campaign['establishment_phone'] ?? '-').toString()} | Pharmacie: ${(campaign['pharmacy_service'] ?? '-').toString()} | Bureau de marche: ${(campaign['market_office_service'] ?? '-').toString()}',
              style: const TextStyle(fontSize: 12, color: Color(0xFF475467)),
            ),
          ],
        ),
      ),
    );
  }

  Widget _metaBadge(
    String label,
    String value, {
    Color? backgroundColor,
    Color? borderColor,
    Color? textColor,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: backgroundColor ?? const Color(0xFFF2F4F7),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: borderColor ?? const Color(0xFFD0D5DD)),
      ),
      child: Text(
        '$label: ${value.trim().isEmpty ? '-' : value.trim()}',
        style: TextStyle(
          fontSize: 12,
          fontWeight: FontWeight.w600,
          color: textColor,
        ),
      ),
    );
  }

  Future<void> _openTempPdf({
    required List<int> bytes,
    required String preferredName,
    required String fallbackBase,
  }) async {
    final path = await writeTempPdf(
      bytes: bytes,
      preferredName: preferredName,
      fallbackBase: fallbackBase,
      prefix: 'trial',
    );
    await _openLocalFile(path);
  }

  Future<void> _openInvoiceFile(dynamic offer) async {
    final invoicePath = (offer['invoice_file_path'] ?? '').toString().trim();
    final invoiceFile = (offer['invoice_file_name'] ?? '').toString().trim();
    final invoiceIdRaw = offer['invoice_id'];
    final invoiceId = invoiceIdRaw is num ? invoiceIdRaw.toInt() : null;

    if (invoicePath.isNotEmpty &&
        (_isLocalWindowsPath(invoicePath) ||
            invoicePath.startsWith('http://') ||
            invoicePath.startsWith('https://'))) {
      final opened = await _tryOpenLocalFile(invoicePath);
      if (opened) {
        return;
      }
    }

    if (invoiceId != null) {
      try {
        final bytes = await CampaignsApi.downloadInvoiceFile(invoiceId);
        await _openTempPdf(
          bytes: bytes,
          preferredName: invoiceFile,
          fallbackBase: 'invoice_$invoiceId',
        );
        return;
      } catch (err) {
        if (!mounted) return;
        _showError(err);
        return;
      }
    }

    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Facture introuvable')),
    );
  }

  Future<void> _openTechSheetFile(dynamic offer) async {
    final techPath = (offer['tech_sheet_file_path'] ?? '').toString().trim();
    final techFile = (offer['tech_sheet_file'] ?? '').toString().trim();
    final techIdRaw = offer['tech_sheet_id'];
    final techId = techIdRaw is num ? techIdRaw.toInt() : null;

    if (techPath.isNotEmpty &&
        (_isLocalWindowsPath(techPath) ||
            techPath.startsWith('http://') ||
            techPath.startsWith('https://'))) {
      final opened = await _tryOpenLocalFile(techPath);
      if (opened) {
        return;
      }
    }

    if (techId != null) {
      try {
        final bytes = await CampaignsApi.downloadTechSheetFile(techId);
        await _openTempPdf(
          bytes: bytes,
          preferredName: techFile,
          fallbackBase: 'tech_sheet_$techId',
        );
        return;
      } catch (err) {
        if (!mounted) return;
        _showError(err);
        return;
      }
    }

    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Fiche technique introuvable')),
    );
  }

  Future<void> _openRequestedSourceAttachment(dynamic product) async {
    final campaignId = _selectedCampaignId;
    final sourcePath =
        (product['requested_source_attachment_path'] ?? '').toString().trim();
    final sourceFile =
        (product['requested_source_attachment_file'] ?? '').toString().trim();
    final sourceIdRaw = product['requested_source_attachment_id'];
    final sourceId = sourceIdRaw is num ? sourceIdRaw.toInt() : null;

    if (sourcePath.isNotEmpty &&
        (_isLocalWindowsPath(sourcePath) ||
            sourcePath.startsWith('http://') ||
            sourcePath.startsWith('https://'))) {
      final opened = await _tryOpenLocalFile(sourcePath);
      if (opened) {
        return;
      }
    }

    if (campaignId != null && sourceId != null) {
      try {
        final bytes = await CampaignsApi.downloadCampaignAttachmentFile(
          campaignId,
          sourceId,
        );
        await _openTempPdf(
          bytes: bytes,
          preferredName: sourceFile,
          fallbackBase: 'campaign_attachment_$sourceId',
        );
        return;
      } catch (err) {
        if (!mounted) return;
        _showError(err);
        return;
      }
    }

    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('PJ source introuvable')),
    );
  }

  String _fmtPercent(double value) {
    final pct = (value * 100).toStringAsFixed(1).replaceAll('.', ',');
    return '$pct%';
  }

  String _fmtValue(dynamic value, {int decimals = 2}) {
    if (value == null) return '-';
    if (value is num) {
      if (value == value.roundToDouble()) return value.toInt().toString();
      return value.toStringAsFixed(decimals).replaceAll('.', ',');
    }
    return value.toString();
  }

  double? _toDouble(dynamic value) {
    if (value is num) return value.toDouble();
    if (value is String) {
      final parsed = double.tryParse(value.trim().replaceAll(',', '.'));
      return parsed;
    }
    return null;
  }

  double? _offerComparablePrice(dynamic offer) {
    final eq = _toDouble(offer['equivalent_unit_price']);
    if (eq != null && eq > 0) return eq;
    return null;
  }

  List<Map<String, dynamic>> _toDetailsList(dynamic detailsRaw) {
    dynamic value = detailsRaw;
    if (value is String) {
      final raw = value.trim();
      if (raw.isNotEmpty) {
        try {
          value = jsonDecode(raw);
        } catch (_) {
          value = null;
        }
      }
    }
    if (value is Map && value['points'] is List) {
      value = value['points'];
    }
    final details = <Map<String, dynamic>>[];
    if (value is List) {
      for (final row in value) {
        if (row is Map) {
          details.add(Map<String, dynamic>.from(row));
        }
      }
    }
    return details;
  }

  (int? matched, int? total) _dynamicCharCountsFromDetails(
    dynamic detailsRaw,
    double threshold,
  ) {
    final details = _toDetailsList(detailsRaw);
    if (details.isEmpty) {
      return (null, null);
    }
    int total = 0;
    int matched = 0;
    for (final row in details) {
      total += 1;
      final score = _toDouble(row['score']);
      bool isMatch = false;
      if (score != null) {
        isMatch = score >= threshold;
      } else {
        isMatch = row['matched'] == true;
      }
      if (isMatch) matched += 1;
    }
    if (total == 0) {
      return (null, null);
    }
    return (matched, total);
  }

  double? _displayEmbeddingScore(dynamic offer) {
    final embStored = _toDouble(offer['embedding_match_rate']);
    if (embStored == null) return null;

    final detailsRaw = offer['characteristics_details'];
    final dynamicCounts =
        _dynamicCharCountsFromDetails(detailsRaw, _charMatchThreshold);
    final dynamicMatched = dynamicCounts.$1;
    final storedMatchedRaw = offer['matched_characteristics'];
    final storedMatched =
        storedMatchedRaw is num ? storedMatchedRaw.toInt() : null;

    // Instant UI update when only characteristics threshold changes:
    // derive base score by removing old bonus then apply new bonus.
    if (dynamicMatched != null && storedMatched != null) {
      final base = (embStored - (0.015 * storedMatched)).clamp(0.0, 1.0);
      final updated = (base + (0.015 * dynamicMatched)).clamp(0.0, 1.0);
      return updated.toDouble();
    }
    return embStored;
  }

  double? _displayGptMatchScore(dynamic offer) {
    final gptRaw = _toDouble(offer['gpt_match_rate']);
    if (gptRaw != null) {
      return gptRaw.clamp(0.0, 1.0).toDouble();
    }
    // Backward compatibility for older rows where gpt score is carried in
    // embedding_match_rate from notes parsing.
    return _displayEmbeddingScore(offer);
  }

  String? _displayMatchScoreSource(dynamic offer) {
    final gptRaw = _toDouble(offer['gpt_match_rate']);
    if (gptRaw != null) return 'GPT';
    final emb = _displayEmbeddingScore(offer);
    if (emb != null) return 'EMBED';
    return null;
  }

  double? _displayTechSimilarityScore(dynamic offer) {
    final display = _toDouble(offer['tech_sheet_similarity_display_score']);
    if (display != null) return display;
    return _toDouble(offer['tech_sheet_similarity_score']);
  }

  Map<String, dynamic>? _techBreakdownMap(dynamic offer) {
    final raw = offer['tech_sheet_similarity_breakdown'];
    if (raw is Map) {
      return Map<String, dynamic>.from(raw);
    }
    if (raw is String) {
      final value = raw.trim();
      if (value.isNotEmpty) {
        try {
          final parsed = jsonDecode(value);
          if (parsed is Map) {
            return Map<String, dynamic>.from(parsed);
          }
        } catch (_) {}
      }
    }
    return null;
  }

  Map<String, dynamic>? _techComponent(dynamic offer, String key) {
    final summaryPrefix = switch (key) {
      'characteristics' => 'tech_characteristics',
      'requirements' => 'tech_requirements',
      'accessories' => 'tech_accessories',
      _ => null,
    };
    if (summaryPrefix != null) {
      final matched = offer['${summaryPrefix}_matched'];
      final total = offer['${summaryPrefix}_total'];
      final coverage = _toDouble(offer['${summaryPrefix}_coverage']);
      if (matched is num || total is num || coverage != null) {
        return {
          'matched': matched,
          'total': total,
          'coverage': coverage,
        };
      }
    }
    if (key == 'description') {
      final descriptionScore = _toDouble(offer['tech_description_score']);
      if (descriptionScore != null) {
        return {
          'score': descriptionScore,
        };
      }
    }
    final breakdown = _techBreakdownMap(offer);
    if (breakdown == null) return null;
    final components = breakdown['components'];
    if (components is! Map) return null;
    final comp = components[key];
    if (comp is! Map) return null;
    return Map<String, dynamic>.from(comp);
  }

  String? _techComponentRatio(dynamic offer, String key) {
    final comp = _techComponent(offer, key);
    if (comp == null) return null;
    final matched = comp['matched'];
    final total = comp['total'];
    if (matched is! num || total is! num) return null;
    return '${matched.toInt()} / ${total.toInt()}';
  }

  String? _techComponentPercent(dynamic offer, String key) {
    final comp = _techComponent(offer, key);
    if (comp == null) return null;
    final coverage = _toDouble(comp['coverage']);
    if (coverage != null) return _fmtPercent(coverage);
    final matched = comp['matched'];
    final total = comp['total'];
    if (matched is num && total is num && total.toDouble() > 0) {
      return _fmtPercent(matched.toDouble() / total.toDouble());
    }
    final score = _toDouble(comp['score']);
    if (score != null) return _fmtPercent(score);
    return null;
  }

  String _ratioWithPercent(String? ratio, String? percent) {
    if (ratio != null &&
        ratio.isNotEmpty &&
        percent != null &&
        percent.isNotEmpty) {
      return '$ratio ($percent)';
    }
    if (ratio != null && ratio.isNotEmpty) return ratio;
    if (percent != null && percent.isNotEmpty) return percent;
    return '-';
  }

  String _techSimilarityStatusLabel(String status) {
    switch (status.toLowerCase()) {
      case 'conforme':
        return 'Conforme';
      case 'partiel':
        return 'Partiel';
      case 'faible':
        return 'Faible';
      case 'no_tech_sheet':
        return 'Sans fiche';
      case 'no_requested_reference':
        return 'Sans reference';
      default:
        return status;
    }
  }

  Future<void> _showTechSimilarityDialog({
    required String requestedName,
    required dynamic offer,
  }) async {
    final breakdown = _techBreakdownMap(offer) ?? const {};
    final hasBreakdown = breakdown.isNotEmpty;
    final componentsRaw = breakdown['components'];
    final components = componentsRaw is Map ? componentsRaw : const {};
    final engine = (breakdown['engine'] ?? '').toString().trim();
    final gptModel = (breakdown['gpt_model'] ?? '').toString().trim();
    final gptReason = (breakdown['gpt_reason'] ?? '').toString().trim();
    final scoreRaw = _toDouble(
      offer['tech_sheet_similarity_display_score'] ??
          offer['tech_sheet_similarity_score'],
    );
    final score = scoreRaw == null ? '-' : _fmtPercent(scoreRaw);
    final status = _techSimilarityStatusLabel(
      (offer['tech_sheet_similarity_display_status'] ??
              offer['tech_sheet_similarity_status'] ??
              '')
          .toString(),
    );
    List<Map<String, dynamic>> componentDetailRows(dynamic component) {
      if (component is! Map) return const [];
      final rows = component['details'];
      if (rows is! List) return const [];
      final out = <Map<String, dynamic>>[];
      for (final row in rows) {
        if (row is Map) out.add(Map<String, dynamic>.from(row));
      }
      return out;
    }

    List<String> collectTextLines(dynamic raw) {
      final out = <String>[];
      if (raw is String) {
        final text = raw.trim();
        if (text.isNotEmpty) out.add(text);
        return out;
      }
      if (raw is List) {
        for (final item in raw) {
          if (item is String) {
            final text = item.trim();
            if (text.isNotEmpty) out.add(text);
          } else if (item is Map) {
            final text =
                (item['text'] ?? item['value'] ?? '').toString().trim();
            if (text.isNotEmpty) out.add(text);
          }
        }
      }
      return out;
    }

    List<String> componentLinesFromPayload(dynamic payload, String key) {
      if (payload is! Map) return const [];
      if (key == 'description') {
        return collectTextLines(payload['description']);
      }
      return collectTextLines(payload[key]);
    }

    Widget metricPill({
      required IconData icon,
      required String label,
      required String value,
      required Color color,
    }) {
      return Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(
          color: color.withValues(alpha: 0.10),
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: color.withValues(alpha: 0.30)),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 14, color: color),
            const SizedBox(width: 5),
            Text(
              '$label: $value',
              style: TextStyle(
                fontSize: 12,
                fontWeight: FontWeight.w600,
                color: color,
              ),
            ),
          ],
        ),
      );
    }

    Widget buildDetailItem({
      required Map<String, dynamic> row,
      required bool matched,
    }) {
      final requiredText =
          (row['required'] ?? row['requested'] ?? '').toString().trim();
      final offeredText =
          (row['best_offered'] ?? row['offered'] ?? '').toString().trim();
      final rowScore = _toDouble(row['score']);
      final scoreLine =
          rowScore == null ? '-' : _fmtPercent(rowScore.clamp(0, 1));
      final rowColor =
          matched ? const Color(0xFF0F766E) : const Color(0xFFB91C1C);
      return Container(
        margin: const EdgeInsets.only(bottom: 6),
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 7),
        decoration: BoxDecoration(
          color: rowColor.withValues(alpha: 0.08),
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: rowColor.withValues(alpha: 0.28)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${matched ? "[OK]" : "[KO]"} ${matched ? "MATCH" : "NON MATCH"} | score: $scoreLine',
              style: TextStyle(
                fontSize: 12,
                fontWeight: FontWeight.w700,
                color: rowColor,
              ),
            ),
            const SizedBox(height: 3),
            Text(
              'Demande: ${requiredText.isEmpty ? "-" : requiredText}',
              style: const TextStyle(fontSize: 12, color: Color(0xFF102A43)),
            ),
            const SizedBox(height: 2),
            Text(
              'Offert: ${offeredText.isEmpty ? "-" : offeredText}',
              style: const TextStyle(fontSize: 12, color: Color(0xFF334E68)),
            ),
          ],
        ),
      );
    }

    Widget componentRow(String key, String title) {
      final raw = components[key];
      final map =
          raw is Map ? Map<String, dynamic>.from(raw) : <String, dynamic>{};
      final scoreValue = _toDouble(map['score']);
      final scoreText = scoreValue == null ? '-' : _fmtPercent(scoreValue);

      final matched = map['matched'];
      final total = map['total'];
      String ratio = '-';
      String coverageText = '-';
      if (matched is num && total is num) {
        final matchedInt = matched.toInt();
        final totalInt = total.toInt();
        ratio = '$matchedInt / $totalInt';
        if (totalInt > 0) coverageText = _fmtPercent(matchedInt / totalInt);
      } else {
        final coverage = _toDouble(map['coverage']);
        if (coverage != null) coverageText = _fmtPercent(coverage);
      }

      final detailRows = componentDetailRows(map);
      final matchedRows =
          detailRows.where((row) => row['matched'] == true).toList();
      final unmatchedRows =
          detailRows.where((row) => row['matched'] != true).toList();
      final requestedLines =
          componentLinesFromPayload(breakdown['requested'], key);
      final offeredLines = componentLinesFromPayload(breakdown['offered'], key);
      final hasAnyDetail = detailRows.isNotEmpty ||
          requestedLines.isNotEmpty ||
          offeredLines.isNotEmpty;

      return Container(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: const Color(0xFFFFFFFF),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: const Color(0xFFD9E2EC)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    title,
                    style: const TextStyle(
                      fontWeight: FontWeight.w700,
                      fontSize: 14,
                      color: Color(0xFF102A43),
                    ),
                  ),
                ),
                metricPill(
                  icon: Icons.checklist_rtl_outlined,
                  label: 'Couverture',
                  value: '$ratio ($coverageText)',
                  color: const Color(0xFF1D4ED8),
                ),
                const SizedBox(width: 8),
                metricPill(
                  icon: Icons.smart_toy_outlined,
                  label: 'Score',
                  value: scoreText,
                  color: const Color(0xFF0F766E),
                ),
              ],
            ),
            if (hasAnyDetail) ...[
              const SizedBox(height: 8),
              Theme(
                data: Theme.of(context)
                    .copyWith(dividerColor: Colors.transparent),
                child: ExpansionTile(
                  tilePadding: EdgeInsets.zero,
                  childrenPadding: EdgeInsets.zero,
                  title: const Text('Voir détails'),
                  subtitle: Text(
                    detailRows.isNotEmpty
                        ? '${matchedRows.length} matchés | ${unmatchedRows.length} non matchés'
                        : 'Détails textuels',
                  ),
                  children: [
                    if (detailRows.isNotEmpty) ...[
                      if (matchedRows.isNotEmpty) ...[
                        const Align(
                          alignment: Alignment.centerLeft,
                          child: Text(
                            'Matchés',
                            style: TextStyle(
                              fontWeight: FontWeight.w700,
                              color: Color(0xFF0F766E),
                            ),
                          ),
                        ),
                        const SizedBox(height: 6),
                        ...matchedRows.take(12).map(
                              (row) => buildDetailItem(
                                row: row,
                                matched: true,
                              ),
                            ),
                      ],
                      if (unmatchedRows.isNotEmpty) ...[
                        const SizedBox(height: 6),
                        const Align(
                          alignment: Alignment.centerLeft,
                          child: Text(
                            'Non matchés',
                            style: TextStyle(
                              fontWeight: FontWeight.w700,
                              color: Color(0xFFB91C1C),
                            ),
                          ),
                        ),
                        const SizedBox(height: 6),
                        ...unmatchedRows.take(12).map(
                              (row) => buildDetailItem(
                                row: row,
                                matched: false,
                              ),
                            ),
                      ],
                      if (detailRows.length > 24)
                        Text(
                          '... ${detailRows.length - 24} autres lignes',
                          style: const TextStyle(
                            fontSize: 12,
                            color: Color(0xFF627D98),
                          ),
                        ),
                    ] else ...[
                      if (requestedLines.isNotEmpty)
                        Text(
                          'Demande: ${requestedLines.join(" ; ")}',
                          style: const TextStyle(
                            fontSize: 12,
                            color: Color(0xFF334E68),
                          ),
                        ),
                      if (offeredLines.isNotEmpty) ...[
                        const SizedBox(height: 4),
                        Text(
                          'Offert: ${offeredLines.join(" ; ")}',
                          style: const TextStyle(
                            fontSize: 12,
                            color: Color(0xFF334E68),
                          ),
                        ),
                      ],
                    ],
                  ],
                ),
              ),
            ],
          ],
        ),
      );
    }

    final debugPayload = <String, dynamic>{
      'engine': engine,
      'gpt_model': gptModel,
      'gpt_reason': gptReason,
      'status': offer['tech_sheet_similarity_display_status'] ??
          offer['tech_sheet_similarity_status'],
      'score_display': offer['tech_sheet_similarity_display_score'],
      'score_raw': offer['tech_sheet_similarity_score'],
      'breakdown': hasBreakdown ? breakdown : null,
    };
    final debugJson = const JsonEncoder.withIndent('  ').convert(debugPayload);
    final debugPreview = debugJson.length > 12000
        ? '${debugJson.substring(0, 12000)}\n... [tronqué]'
        : debugJson;

    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Details conformite (GPT)'),
        content: SizedBox(
          width: 860,
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: const Color(0xFFF8FAFC),
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: const Color(0xFFD9E2EC)),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Produit demandé: $requestedName',
                        style: const TextStyle(
                          fontWeight: FontWeight.w700,
                          color: Color(0xFF102A43),
                        ),
                      ),
                      const SizedBox(height: 8),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          metricPill(
                            icon: Icons.verified_outlined,
                            label: 'Statut',
                            value: status,
                            color: const Color(0xFF1D4ED8),
                          ),
                          metricPill(
                            icon: Icons.analytics_outlined,
                            label: 'Conformité globale',
                            value: score,
                            color: const Color(0xFF0F766E),
                          ),
                          metricPill(
                            icon: Icons.checklist_rtl_outlined,
                            label: 'Caract.',
                            value: _ratioWithPercent(
                              _techComponentRatio(offer, 'characteristics'),
                              _techComponentPercent(offer, 'characteristics'),
                            ),
                            color: const Color(0xFF4C6FFF),
                          ),
                          metricPill(
                            icon: Icons.assignment_outlined,
                            label: 'Exigences',
                            value: _ratioWithPercent(
                              _techComponentRatio(offer, 'requirements'),
                              _techComponentPercent(offer, 'requirements'),
                            ),
                            color: const Color(0xFF7C3AED),
                          ),
                          metricPill(
                            icon: Icons.precision_manufacturing_outlined,
                            label: 'Accessoires',
                            value: _ratioWithPercent(
                              _techComponentRatio(offer, 'accessories'),
                              _techComponentPercent(offer, 'accessories'),
                            ),
                            color: const Color(0xFF1D4ED8),
                          ),
                          metricPill(
                            icon: Icons.description_outlined,
                            label: 'Description',
                            value:
                                _techComponentPercent(offer, 'description') ??
                                    '-',
                            color: const Color(0xFF0F766E),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 10),
                if (!hasBreakdown)
                  Container(
                    width: double.infinity,
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: const Color(0xFFF4F8FF),
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(color: const Color(0xFFD7E5FF)),
                    ),
                    child: Text(
                      () {
                        final rawStatus =
                            (offer['tech_sheet_similarity_display_status'] ??
                                    offer['tech_sheet_similarity_status'] ??
                                    '')
                                .toString()
                                .toLowerCase()
                                .trim();
                        if (rawStatus == 'no_tech_sheet') {
                          return 'Aucune fiche technique liee a cette offre. Les details GPT ne peuvent pas etre calcules.';
                        }
                        if (rawStatus == 'no_requested_reference') {
                          return 'Reference produit demandee introuvable. Les details GPT ne peuvent pas etre calcules.';
                        }
                        return 'Details GPT non disponibles pour cette offre.';
                      }(),
                      style: const TextStyle(color: Color(0xFF334E68)),
                    ),
                  )
                else ...[
                  componentRow('characteristics', 'Caracteristiques'),
                  componentRow('requirements', 'Exigences'),
                  componentRow('accessories', 'Accessoires'),
                  componentRow('description', 'Description'),
                ],
                const SizedBox(height: 8),
                Theme(
                  data: Theme.of(context)
                      .copyWith(dividerColor: Colors.transparent),
                  child: ExpansionTile(
                    tilePadding: EdgeInsets.zero,
                    childrenPadding: EdgeInsets.zero,
                    title: const Text('Technique (debug)'),
                    subtitle: const Text('JSON GPT + métadonnées'),
                    children: [
                      Container(
                        width: double.infinity,
                        padding: const EdgeInsets.all(10),
                        decoration: BoxDecoration(
                          color: const Color(0xFFF8FAFC),
                          borderRadius: BorderRadius.circular(8),
                          border: Border.all(color: const Color(0xFFD9E2EC)),
                        ),
                        child: SelectableText(
                          debugPreview,
                          style: const TextStyle(
                            fontSize: 12,
                            color: Color(0xFF334E68),
                            fontFamily: 'monospace',
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Fermer'),
          ),
        ],
      ),
    );
  }

  @override
  void initState() {
    super.initState();
    _productListController.addListener(_handleProductListScroll);
    unawaited(_bootstrap());
  }

  @override
  void dispose() {
    _thresholdAutoApplyTimer?.cancel();
    _uiStatePersistTimer?.cancel();
    _rememberCurrentCampaignUiState();
    _rememberVisibleOffersForCurrentCampaign();
    unawaited(_persistUiState());
    _productSearchController.dispose();
    _productListController.dispose();
    super.dispose();
  }

  Future<void> _bootstrap() async {
    await _loadPersistedUiState();
    await _load();
  }

  int _countOffersInProducts(List<dynamic> rows) {
    var total = 0;
    for (final raw in rows) {
      if (raw is! Map) continue;
      final offers = raw['offers'];
      if (offers is List) {
        total += offers.length;
      }
    }
    return total;
  }

  int _tabIndexForCampaign(int? campaignId) {
    final value =
        campaignId == null ? 0 : (_activeTabByCampaign[campaignId] ?? 0);
    return value.clamp(0, 1);
  }

  void _setActiveTabForCurrentCampaign(int index) {
    final campaignId = _selectedCampaignId;
    if (campaignId == null) return;
    _activeTabByCampaign[campaignId] = index.clamp(0, 1);
    _schedulePersistUiState();
  }

  String _campaignTitleById(int campaignId) {
    for (final row in _campaigns) {
      if (row is! Map) continue;
      final idRaw = row['id'];
      if (idRaw is num && idRaw.toInt() == campaignId) {
        return (row['title'] ?? 'Campagne $campaignId').toString();
      }
    }
    return 'Campagne $campaignId';
  }

  bool _campaignExists(int? campaignId) {
    if (campaignId == null) return false;
    for (final row in _campaigns) {
      if (row is! Map) continue;
      final idRaw = row['id'];
      if (idRaw is num && idRaw.toInt() == campaignId) {
        return true;
      }
    }
    return false;
  }

  void _rememberVisibleOffersForCurrentCampaign() {
    final campaignId = _selectedCampaignId;
    if (campaignId == null) return;
    _visibleOffersByCampaign[campaignId] =
        Map<int, int>.from(_visibleOffersByProduct);
    _schedulePersistUiState();
  }

  void _restoreVisibleOffersForCampaign(int? campaignId) {
    _visibleOffersByProduct
      ..clear()
      ..addAll(
          Map<int, int>.from(_visibleOffersByCampaign[campaignId] ?? const {}));
  }

  void _handleProductListScroll() {
    _rememberScrollOffsetForCurrentCampaign();
    final shouldShow = _productListController.hasClients &&
        _productListController.offset > 500;
    if (shouldShow != _showBackToTop && mounted) {
      setState(() => _showBackToTop = shouldShow);
    }
  }

  void _rememberScrollOffsetForCurrentCampaign() {
    final campaignId = _selectedCampaignId;
    if (campaignId == null || !_productListController.hasClients) {
      return;
    }
    _productScrollOffsetByCampaign[campaignId] = _productListController.offset;
    _schedulePersistUiState();
  }

  void _rememberCurrentCampaignUiState() {
    final campaignId = _selectedCampaignId;
    if (campaignId == null) return;
    _productQueryByCampaign[campaignId] = _productQuery;
    _rememberScrollOffsetForCurrentCampaign();
    _schedulePersistUiState();
  }

  void _restoreCampaignUiState(int? campaignId) {
    final restoredQuery =
        campaignId == null ? '' : (_productQueryByCampaign[campaignId] ?? '');
    _productQuery = restoredQuery;
    if (_productSearchController.text != restoredQuery) {
      _productSearchController.value = TextEditingValue(
        text: restoredQuery,
        selection: TextSelection.collapsed(offset: restoredQuery.length),
      );
    }
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      if (campaignId == null || _selectedCampaignId != campaignId) return;
      if (!_productListController.hasClients) return;
      final target = _productScrollOffsetByCampaign[campaignId] ?? 0.0;
      final maxScroll = _productListController.position.maxScrollExtent;
      final clamped = target.clamp(0.0, maxScroll).toDouble();
      if ((_productListController.offset - clamped).abs() > 2) {
        _productListController.jumpTo(clamped);
      }
    });
  }

  Future<void> _changeSelectedCampaign(int campaignId) async {
    if (_selectedCampaignId == campaignId) {
      await _loadDetails();
      return;
    }
    _rememberCurrentCampaignUiState();
    _rememberVisibleOffersForCurrentCampaign();
    if (_selectedCampaignId != null && _selectedCampaignId != campaignId) {
      _resumeCampaignId = _selectedCampaignId;
    }
    setState(() {
      _selectedCampaignId = campaignId;
      _lastSelectedCampaignId = campaignId;
      _restoreVisibleOffersForCampaign(campaignId);
    });
    _schedulePersistUiState();
    _restoreCampaignUiState(campaignId);
    await _loadDetails();
  }

  Map<int, String> _parseStringMap(dynamic raw) {
    final out = <int, String>{};
    if (raw is! Map) return out;
    raw.forEach((key, value) {
      final parsedKey = int.tryParse(key.toString());
      if (parsedKey == null) return;
      out[parsedKey] = value?.toString() ?? '';
    });
    return out;
  }

  Map<int, double> _parseDoubleMap(dynamic raw) {
    final out = <int, double>{};
    if (raw is! Map) return out;
    raw.forEach((key, value) {
      final parsedKey = int.tryParse(key.toString());
      if (parsedKey == null) return;
      final parsedValue = _toDouble(value);
      if (parsedValue == null) return;
      out[parsedKey] = parsedValue;
    });
    return out;
  }

  Map<int, int> _parseIntMap(dynamic raw) {
    final out = <int, int>{};
    if (raw is! Map) return out;
    raw.forEach((key, value) {
      final parsedKey = int.tryParse(key.toString());
      if (parsedKey == null) return;
      final parsedValue =
          value is num ? value.toInt() : int.tryParse(value.toString());
      if (parsedValue == null) return;
      out[parsedKey] = parsedValue;
    });
    return out;
  }

  Map<int, Map<int, int>> _parseNestedIntMap(dynamic raw) {
    final out = <int, Map<int, int>>{};
    if (raw is! Map) return out;
    raw.forEach((key, value) {
      final parsedKey = int.tryParse(key.toString());
      if (parsedKey == null) return;
      out[parsedKey] = _parseIntMap(value);
    });
    return out;
  }

  Map<int, Map<String, dynamic>> _parseNestedDynamicMap(dynamic raw) {
    final out = <int, Map<String, dynamic>>{};
    if (raw is! Map) return out;
    raw.forEach((key, value) {
      final parsedKey = int.tryParse(key.toString());
      if (parsedKey == null || value is! Map) return;
      out[parsedKey] = Map<String, dynamic>.from(value);
    });
    return out;
  }

  Future<void> _loadPersistedUiState() async {
    try {
      final raw = await _uiStateStorage.read(key: _uiStateStorageKey);
      if (raw == null || raw.trim().isEmpty) return;
      final parsed = jsonDecode(raw);
      if (parsed is! Map) return;
      _productQueryByCampaign
        ..clear()
        ..addAll(_parseStringMap(parsed['product_query_by_campaign']));
      _productScrollOffsetByCampaign
        ..clear()
        ..addAll(_parseDoubleMap(parsed['product_scroll_offset_by_campaign']));
      _activeTabByCampaign
        ..clear()
        ..addAll(_parseIntMap(parsed['active_tab_by_campaign']));
      _visibleOffersByCampaign
        ..clear()
        ..addAll(_parseNestedIntMap(parsed['visible_offers_by_campaign']));
      _futureFiltersByCampaign
        ..clear()
        ..addAll(_parseNestedDynamicMap(parsed['future_filters_by_campaign']));
      final lastSelectedRaw = parsed['last_selected_campaign_id'];
      if (lastSelectedRaw is num) {
        _lastSelectedCampaignId = lastSelectedRaw.toInt();
      } else if (lastSelectedRaw is String) {
        _lastSelectedCampaignId = int.tryParse(lastSelectedRaw);
      }
      final resumeRaw = parsed['resume_campaign_id'];
      if (resumeRaw is num) {
        _resumeCampaignId = resumeRaw.toInt();
      } else if (resumeRaw is String) {
        _resumeCampaignId = int.tryParse(resumeRaw);
      }
    } catch (_) {}
  }

  void _schedulePersistUiState() {
    _uiStatePersistTimer?.cancel();
    _uiStatePersistTimer = Timer(const Duration(milliseconds: 500), () {
      unawaited(_persistUiState());
    });
  }

  Future<void> _persistUiState() async {
    try {
      final payload = {
        'product_query_by_campaign': {
          for (final entry in _productQueryByCampaign.entries)
            entry.key.toString(): entry.value,
        },
        'product_scroll_offset_by_campaign': {
          for (final entry in _productScrollOffsetByCampaign.entries)
            entry.key.toString(): entry.value,
        },
        'active_tab_by_campaign': {
          for (final entry in _activeTabByCampaign.entries)
            entry.key.toString(): entry.value,
        },
        'visible_offers_by_campaign': {
          for (final campaignEntry in _visibleOffersByCampaign.entries)
            campaignEntry.key.toString(): {
              for (final productEntry in campaignEntry.value.entries)
                productEntry.key.toString(): productEntry.value,
            },
        },
        'future_filters_by_campaign': {
          for (final entry in _futureFiltersByCampaign.entries)
            entry.key.toString(): entry.value,
        },
        'last_selected_campaign_id': _lastSelectedCampaignId,
        'resume_campaign_id': _resumeCampaignId,
      };
      await _uiStateStorage.write(
        key: _uiStateStorageKey,
        value: jsonEncode(payload),
      );
    } catch (_) {}
  }

  void _applyOffersPage(
    Map<String, dynamic> payload, {
    required bool append,
  }) {
    final items = List<dynamic>.from((payload['items'] as List?) ?? const []);
    final pageRaw = payload['page'];
    final totalRaw = payload['total'];
    final page = pageRaw is num ? pageRaw.toInt() : 1;
    final total = totalRaw is num ? totalRaw.toInt() : items.length;
    final hasMore = payload['has_more'] == true;
    final selectedCampaignId = _selectedCampaignId;
    _conformOffers =
        append ? <dynamic>[..._conformOffers, ...items] : <dynamic>[...items];
    if (!append && _loadedOffersCampaignId != selectedCampaignId) {
      _visibleOffersByProduct.clear();
    }
    for (final raw in items) {
      if (raw is! Map) continue;
      final productIdRaw = raw['product_id'];
      final offers = raw['offers'];
      if (productIdRaw is! num || offers is! List) continue;
      final productId = productIdRaw.toInt();
      _visibleOffersByProduct.putIfAbsent(
        productId,
        () => offers.length < _initialOffersVisible
            ? offers.length
            : _initialOffersVisible,
      );
    }
    _rememberVisibleOffersForCurrentCampaign();
    _loadedOffersCampaignId = selectedCampaignId;
    _offersCurrentPage = page;
    _offersTotal = total;
    _offersHasMore = hasMore;
  }

  int _visibleOfferCount(int productId, int offersLength) {
    final configured = _visibleOffersByProduct[productId];
    if (configured == null) {
      return offersLength < _initialOffersVisible
          ? offersLength
          : _initialOffersVisible;
    }
    return configured.clamp(0, offersLength);
  }

  Future<void> _openTechSimilarityDetails({
    required String requestedName,
    required int productId,
    required dynamic offer,
  }) async {
    final invoiceItemIdRaw = offer['invoice_item_id'];
    final detailOfferId =
        invoiceItemIdRaw is num ? invoiceItemIdRaw.toInt() : null;
    if (detailOfferId != null && detailOfferId > 0) {
      if (_loadingTechDetailOfferIds.contains(detailOfferId)) {
        return;
      }
      if (mounted) {
        setState(() => _loadingTechDetailOfferIds.add(detailOfferId));
      }
    }
    dynamic effectiveOffer = offer;
    final hasBreakdown = _techBreakdownMap(offer) != null;
    final campaignId = _selectedCampaignId;
    try {
      if (!hasBreakdown &&
          campaignId != null &&
          invoiceItemIdRaw is num &&
          invoiceItemIdRaw.toInt() > 0) {
        final detail = await CampaignsApi.offerByProductDetail(
          campaignId,
          productId,
          invoiceItemIdRaw.toInt(),
        );
        effectiveOffer = {
          ...Map<String, dynamic>.from(offer as Map),
          ...detail,
        };
      }
      if (!mounted) return;
      await _showTechSimilarityDialog(
        requestedName: requestedName,
        offer: effectiveOffer,
      );
    } catch (err) {
      if (!mounted) return;
      _showError(err);
    } finally {
      if (detailOfferId != null && detailOfferId > 0 && mounted) {
        setState(() => _loadingTechDetailOfferIds.remove(detailOfferId));
      }
    }
  }

  Future<void> _autoSelectCampaignWithOffers() async {
    if (_campaigns.isEmpty) return;
    if (_countOffersInProducts(_conformOffers) > 0) return;
    final currentCampaignId = _selectedCampaignId;
    var scanned = 0;
    for (final row in _campaigns) {
      if (scanned >= 5) break;
      if (row is! Map) continue;
      final idRaw = row['id'];
      if (idRaw is! num) continue;
      final id = idRaw.toInt();
      if (currentCampaignId != null && id == currentCampaignId) continue;
      scanned += 1;
      try {
        final offersPage = await CampaignsApi.offersByProduct(
          id,
          conformOnly: false,
          page: 1,
          pageSize: _offersPageSize,
        ).timeout(const Duration(seconds: 12));
        final offers =
            List<dynamic>.from((offersPage['items'] as List?) ?? const []);
        if (_countOffersInProducts(offers) > 0) {
          if (!mounted) return;
          await _changeSelectedCampaign(id);
          if (!mounted) return;
          final title = (row['title'] ?? 'Campagne $id').toString();
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text(
                  'Campagne chargee automatiquement: $title (offres disponibles)'),
            ),
          );
          return;
        }
      } catch (_) {
        continue;
      }
    }
  }

  Future<void> _load() async {
    _rememberCurrentCampaignUiState();
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      _campaigns = await CampaignsApi.list();
      final suppliers = await SuppliersApi.list();
      final thresholds = await SettingsApi.getEmbeddingThresholds();
      _embedConfirm = (thresholds['confirm_threshold'] as num?)?.toDouble() ??
          AppThresholds.defaultConfirmThreshold;
      _embedVerify = AppThresholds.fixedVerifyThreshold;
      // Characteristics threshold is intentionally fixed on UI.
      _charMatchThreshold = _charThresholdFixed;
      _savedEmbedConfirm = _embedConfirm;
      _savedCharMatchThreshold = _charMatchThreshold;
      _supplierNames = {
        for (final s in suppliers) s['id'] as int: s['name'] as String,
      };
      if (_campaigns.isNotEmpty) {
        final restoredCampaignId = _campaignExists(_lastSelectedCampaignId)
            ? _lastSelectedCampaignId
            : (_campaigns.first['id'] as int);
        _selectedCampaignId = restoredCampaignId;
        _lastSelectedCampaignId = restoredCampaignId;
        _restoreVisibleOffersForCampaign(_selectedCampaignId);
        _restoreCampaignUiState(_selectedCampaignId);
      }
    } catch (err) {
      _error = ApiClient.formatError(err);
    }
    if (mounted) {
      setState(() => _loading = false);
    }
    if (_selectedCampaignId != null) {
      unawaited(_loadDetails());
    }
    // Keep the user-selected campaign; do not auto-switch to another campaign
    // just because it has offers.
  }

  Future<void> _saveThresholds({bool silent = false}) async {
    if (_savingThresholds) return;
    bool changed(double a, double b) => (a - b).abs() > 1e-9;
    final changedConfirm = changed(_embedConfirm, _savedEmbedConfirm);
    const changedVerify = false;
    final changedChar = changed(_charMatchThreshold, _savedCharMatchThreshold);
    final hasChanges = changedConfirm || changedVerify || changedChar;
    if (!hasChanges) {
      if (!silent && mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
              content: Text('Aucun changement de seuil a enregistrer.')),
        );
      }
      return;
    }
    setState(() => _savingThresholds = true);
    try {
      _charMatchThreshold = _charThresholdFixed;
      await SettingsApi.updateEmbeddingThresholds({
        'confirm_threshold': _embedConfirm,
        'verify_threshold': AppThresholds.fixedVerifyThreshold,
        'characteristics_threshold': _charMatchThreshold,
      });
      _savedEmbedConfirm = _embedConfirm;
      _savedCharMatchThreshold = _charMatchThreshold;
      final needsFull = changedConfirm || changedVerify;
      final needsCharOnly = !needsFull && changedChar;
      _enqueueThresholdRecompute(
        silent: silent,
        full: needsFull,
        charOnly: needsCharOnly,
      );
      if (!silent && mounted) {
        final text = needsFull
            ? 'Seuils enregistres. Recalcul complet en cours...'
            : 'Seuils enregistres. Recalcul caracteristiques en cours...';
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(text)),
        );
      }
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    } finally {
      if (mounted) {
        setState(() => _savingThresholds = false);
      }
    }
  }

  void _enqueueThresholdRecompute({
    required bool silent,
    required bool full,
    required bool charOnly,
  }) {
    if (_selectedCampaignId == null) return;
    if (full) {
      _thresholdRecomputeNeedsFull = true;
      _thresholdRecomputeNeedsCharOnly = false;
    } else if (charOnly) {
      _thresholdRecomputeNeedsCharOnly = true;
    }
    if (_thresholdRecomputeRunning) return;
    unawaited(_runThresholdRecomputeQueue(silent: silent));
  }

  Future<void> _runThresholdRecomputeQueue({required bool silent}) async {
    _thresholdRecomputeRunning = true;
    try {
      while (_thresholdRecomputeNeedsFull || _thresholdRecomputeNeedsCharOnly) {
        final runFull = _thresholdRecomputeNeedsFull;
        final runCharOnly = !runFull && _thresholdRecomputeNeedsCharOnly;
        _thresholdRecomputeNeedsFull = false;
        _thresholdRecomputeNeedsCharOnly = false;
        final campaignId = _selectedCampaignId;
        if (campaignId == null) {
          break;
        }
        if (runFull) {
          await InvoiceExtractionsApi.recompute(campaignId);
        } else if (runCharOnly) {
          await InvoiceExtractionsApi.recomputeCharacteristics(campaignId);
        }
      }
      await _loadDetails();
      if (!silent && mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Recalcul automatique termine.')),
        );
      }
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    } finally {
      _thresholdRecomputeRunning = false;
    }
  }

  void _scheduleAutoApplyThresholds() {
    _thresholdAutoApplyTimer?.cancel();
    _thresholdAutoApplyTimer = Timer(const Duration(milliseconds: 800), () {
      if (!mounted) return;
      _saveThresholds(silent: true);
    });
  }

  Future<void> _loadDetails() async {
    if (_selectedCampaignId == null) return;
    _rememberCurrentCampaignUiState();
    final id = _selectedCampaignId!;
    try {
      final results = await Future.wait<dynamic>([
        CampaignsApi.stats(id),
        CampaignsApi.offersByProduct(
          id,
          conformOnly: false,
          page: 1,
          pageSize: _offersPageSize,
        ),
        CampaignsApi.recipients(id),
        CampaignsApi.responses(id),
      ]);
      final stats = Map<String, dynamic>.from(results[0] as Map);
      final offersPage = Map<String, dynamic>.from(results[1] as Map);
      final recipients = List<dynamic>.from(results[2] as List);
      final responses = List<dynamic>.from(results[3] as List);
      setState(() {
        _stats = stats;
        _applyOffersPage(offersPage, append: false);
        _recipients = recipients;
        _responses = responses;
      });
      _restoreCampaignUiState(id);
    } catch (err) {
      if (!mounted) return;
      _showError(err);
    }
  }

  Future<void> _loadMoreOffers() async {
    final campaignId = _selectedCampaignId;
    if (campaignId == null || _offersLoadingMore || !_offersHasMore) {
      return;
    }
    setState(() => _offersLoadingMore = true);
    try {
      final payload = await CampaignsApi.offersByProduct(
        campaignId,
        conformOnly: false,
        page: _offersCurrentPage + 1,
        pageSize: _offersPageSize,
      );
      if (!mounted) return;
      setState(() {
        _applyOffersPage(payload, append: true);
      });
    } catch (err) {
      if (!mounted) return;
      _showError(err);
    } finally {
      if (mounted) {
        setState(() => _offersLoadingMore = false);
      }
    }
  }

  Future<void> _exportCsv() async {
    final id = _selectedCampaignId;
    if (id == null) return;
    try {
      final bytes = await CampaignsApi.exportCsv(id);
      final campaign = _campaigns.firstWhere(
        (c) => c['id'] == id,
        orElse: () => null,
      );
      final title =
          campaign == null ? 'campaign_$id' : (campaign['title'] as String);
      final fileName = '${title.replaceAll(' ', '_')}_responses.csv';
      final path = await FilePicker.platform.saveFile(
        dialogTitle: 'Save CSV export',
        fileName: fileName,
        type: FileType.custom,
        allowedExtensions: ['csv'],
      );
      if (path == null) return;
      final savePath = _ensureFileExtension(path, 'csv');
      await File(savePath).writeAsBytes(bytes, flush: true);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Exported to $savePath')),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  Future<void> _exportTopOffersXlsx() async {
    final id = _selectedCampaignId;
    if (id == null) return;
    try {
      final bytes = await CampaignsApi.exportTopOffersXlsx(id);
      final campaign = _campaigns.firstWhere(
        (c) => c['id'] == id,
        orElse: () => null,
      );
      final title =
          campaign == null ? 'campaign_$id' : (campaign['title'] as String);
      final fileName = '${title.replaceAll(' ', '_')}_top_offers.xlsx';
      final path = await FilePicker.platform.saveFile(
        dialogTitle: 'Save Excel export',
        fileName: fileName,
        type: FileType.custom,
        allowedExtensions: ['xlsx'],
      );
      if (path == null) return;
      final savePath = _ensureFileExtension(path, 'xlsx');
      await File(savePath).writeAsBytes(bytes, flush: true);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Exported to $savePath')),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return Center(child: Text('Error: $_error'));
    }
    return DefaultTabController(
      key: ValueKey<int>(_selectedCampaignId ?? 0),
      initialIndex: _tabIndexForCampaign(_selectedCampaignId),
      length: 2,
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                gradient: const LinearGradient(
                  colors: [Color(0xFFF4F8FF), Color(0xFFFFFFFF)],
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                ),
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: const Color(0xFFD7E5FF)),
              ),
              child: Wrap(
                spacing: 12,
                runSpacing: 10,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  const Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(Icons.insights_outlined, color: Color(0xFF0B3D91)),
                      SizedBox(width: 8),
                      Text(
                        'Results',
                        style: TextStyle(
                          fontSize: 22,
                          fontWeight: FontWeight.bold,
                          color: Color(0xFF0B2545),
                        ),
                      ),
                    ],
                  ),
                  if (_campaigns.isNotEmpty)
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 10),
                      decoration: BoxDecoration(
                        color: Colors.white,
                        borderRadius: BorderRadius.circular(10),
                        border: Border.all(color: const Color(0xFFCFD8E3)),
                      ),
                      child: DropdownButtonHideUnderline(
                        child: DropdownButton<int>(
                          value: _selectedCampaignId,
                          items: _campaigns
                              .map(
                                (c) => DropdownMenuItem<int>(
                                  value: c['id'] as int,
                                  child: Text(c['title'] as String),
                                ),
                              )
                              .toList(),
                          onChanged: (value) async {
                            if (value == null) return;
                            await _changeSelectedCampaign(value);
                          },
                        ),
                      ),
                    ),
                  if (_selectedCampaignId != null)
                    FilledButton.tonalIcon(
                      onPressed: _exportCsv,
                      icon: const Icon(Icons.file_download_outlined),
                      label: const Text('Export CSV'),
                    ),
                  if (_selectedCampaignId != null)
                    FilledButton.tonalIcon(
                      onPressed: _exportTopOffersXlsx,
                      icon: const Icon(Icons.table_chart_outlined),
                      label: const Text('Exporter Excel (Top offres)'),
                    ),
                  if (_campaignExists(_resumeCampaignId) &&
                      _resumeCampaignId != null &&
                      _resumeCampaignId != _selectedCampaignId)
                    FilledButton.tonalIcon(
                      onPressed: () async {
                        final resumeCampaignId = _resumeCampaignId;
                        if (resumeCampaignId == null) return;
                        await _changeSelectedCampaign(resumeCampaignId);
                      },
                      icon: const Icon(Icons.history),
                      label: Text(
                        'Reprendre: ${_campaignTitleById(_resumeCampaignId!)}',
                      ),
                    ),
                  OutlinedButton.icon(
                    onPressed: _load,
                    icon: const Icon(Icons.refresh),
                    label: const Text('Refresh'),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 12),
            _campaignMetaBanner(),
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(10),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      'Seuils Embeddings (Results)',
                      style: TextStyle(fontWeight: FontWeight.bold),
                    ),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        _buildThresholdRow(
                          label: 'Caracteristiques',
                          value: _charMatchThreshold,
                          color: const Color(0xFF6D28D9),
                          minValue: _charThresholdMin,
                          maxValue: _charThresholdFixed,
                          enabled: false,
                          onChanged: (_) {},
                        ),
                        _buildThresholdRow(
                          label: 'Confirme',
                          value: _embedConfirm,
                          color: const Color(0xFF0F766E),
                          onChanged: (v) {
                            setState(() => _embedConfirm = v);
                            _scheduleAutoApplyThresholds();
                          },
                        ),
                        _buildThresholdRow(
                          label: 'A verifier (fixe 40%)',
                          value: _embedVerify,
                          color: const Color(0xFFB45309),
                          enabled: false,
                          onChanged: (_) {},
                        ),
                      ],
                    ),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 12,
                      children: [
                        FilledButton.icon(
                          onPressed: _savingThresholds
                              ? null
                              : () => _saveThresholds(silent: false),
                          icon: const Icon(Icons.save_outlined),
                          label: Text(_savingThresholds
                              ? 'Enregistrement...'
                              : 'Enregistrer'),
                        ),
                        const Text(
                            'Note: Changement des seuils = application auto (~1s), recalcul en arriere-plan.'),
                      ],
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 12),
            TabBar(
              onTap: _setActiveTabForCurrentCampaign,
              tabs: const [
                Tab(text: 'By product'),
                Tab(text: 'Statistics'),
              ],
            ),
            const SizedBox(height: 12),
            Expanded(
              child: TabBarView(
                children: [
                  _buildByProduct(),
                  _buildStatistics(),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildByProduct() {
    if (_conformOffers.isEmpty) {
      return const EmptyState(
        icon: Icons.bar_chart_outlined,
        title: 'No offers yet',
        message: 'Send a campaign and wait for supplier responses.',
      );
    }

    final screenWidth = MediaQuery.sizeOf(context).width;
    final searchWidth = screenWidth < 1100 ? 280.0 : 340.0;
    final query = _productQuery.trim().toLowerCase();
    List<Map<String, dynamic>> buildDisplayProducts() {
      final out = <Map<String, dynamic>>[];
      for (final raw in _conformOffers) {
        final product = Map<String, dynamic>.from(raw as Map);
        final name = (product['product_name'] ?? '').toString().toLowerCase();
        if (query.isNotEmpty && !name.contains(query)) {
          continue;
        }
        final offers = (product['offers'] as List<dynamic>?) ?? const [];
        final shownOffers = <dynamic>[];
        for (final row in offers) {
          final score = _displayGptMatchScore(row);
          if (score == null || score < _resultsMinGptMatch) {
            continue;
          }
          shownOffers.add(row);
        }
        if (shownOffers.isNotEmpty) {
          shownOffers.sort((a, b) {
            final aPrice = _offerComparablePrice(a) ?? double.infinity;
            final bPrice = _offerComparablePrice(b) ?? double.infinity;
            return aPrice.compareTo(bPrice);
          });
        }
        double? bestConformity;
        dynamic bestConformityOffer;
        for (final row in shownOffers) {
          final score = _displayTechSimilarityScore(row);
          if (score == null) continue;
          if (bestConformity == null || score > bestConformity) {
            bestConformity = score;
            bestConformityOffer = row;
          }
        }
        product['offers'] = shownOffers;
        product['best_offer'] = shownOffers.isEmpty ? null : shownOffers.first;
        product['best_conformity'] = bestConformity;
        product['best_conformity_offer'] = bestConformityOffer;
        out.add(product);
      }
      return out;
    }

    final displayProducts = buildDisplayProducts();
    final totalPages =
        _offersTotal <= 0 ? 1 : ((_offersTotal - 1) ~/ _offersPageSize) + 1;
    final currentPage =
        totalPages <= 0 ? 1 : _offersCurrentPage.clamp(1, totalPages);

    void expandAllVisibleProducts() {
      setState(() {
        for (final product in displayProducts) {
          final productIdRaw = product['product_id'];
          final offers = product['offers'];
          if (productIdRaw is! num || offers is! List) continue;
          _visibleOffersByProduct[productIdRaw.toInt()] = offers.length;
        }
      });
      _rememberVisibleOffersForCurrentCampaign();
    }

    void collapseAllVisibleProducts() {
      setState(() {
        for (final product in displayProducts) {
          final productIdRaw = product['product_id'];
          final offers = product['offers'];
          if (productIdRaw is! num || offers is! List) continue;
          _visibleOffersByProduct[productIdRaw.toInt()] =
              offers.length < _initialOffersVisible
                  ? offers.length
                  : _initialOffersVisible;
        }
      });
      _rememberVisibleOffersForCurrentCampaign();
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 10,
          runSpacing: 10,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            SizedBox(
              width: searchWidth,
              child: TextField(
                controller: _productSearchController,
                decoration: InputDecoration(
                  labelText: 'Search products',
                  hintText: 'Tape un nom de produit',
                  prefixIcon: const Icon(Icons.search),
                  suffixIcon: _productQuery.isEmpty
                      ? null
                      : IconButton(
                          icon: const Icon(Icons.clear),
                          onPressed: () {
                            _productSearchController.clear();
                            setState(() {
                              _productQuery = '';
                              final campaignId = _selectedCampaignId;
                              if (campaignId != null) {
                                _productQueryByCampaign[campaignId] = '';
                              }
                            });
                            _schedulePersistUiState();
                          },
                        ),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                ),
                onChanged: (value) {
                  setState(() {
                    _productQuery = value;
                    final campaignId = _selectedCampaignId;
                    if (campaignId != null) {
                      _productQueryByCampaign[campaignId] = value;
                    }
                  });
                  _schedulePersistUiState();
                },
              ),
            ),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              decoration: BoxDecoration(
                color: const Color(0xFFEAF2FF),
                borderRadius: BorderRadius.circular(999),
              ),
              child: Text(
                '${displayProducts.length} produits affiches | ${_conformOffers.length} charges / $_offersTotal total | page $currentPage / $totalPages',
                style: const TextStyle(
                  fontWeight: FontWeight.w600,
                  color: Color(0xFF0B3D91),
                ),
              ),
            ),
            OutlinedButton.icon(
              onPressed:
                  displayProducts.isEmpty ? null : expandAllVisibleProducts,
              icon: const Icon(Icons.unfold_more),
              label: const Text('Tout ouvrir'),
            ),
            OutlinedButton.icon(
              onPressed:
                  displayProducts.isEmpty ? null : collapseAllVisibleProducts,
              icon: const Icon(Icons.unfold_less),
              label: const Text('Tout reduire'),
            ),
          ],
        ),
        const SizedBox(height: 12),
        Expanded(
          child: displayProducts.isEmpty
              ? const EmptyState(
                  icon: Icons.bar_chart_outlined,
                  title: 'Aucune offre',
                  message: 'Ajuste la recherche produit.',
                )
              : Stack(
                  children: [
                    ListView.builder(
                      controller: _productListController,
                      key: ValueKey<int>(_selectedCampaignId ?? 0),
                      itemCount:
                          displayProducts.length + (_offersHasMore ? 1 : 0),
                      itemBuilder: (context, index) {
                        if (index >= displayProducts.length) {
                          return Padding(
                            padding: const EdgeInsets.only(top: 12, bottom: 24),
                            child: Center(
                              child: FilledButton.tonalIcon(
                                onPressed:
                                    _offersLoadingMore ? null : _loadMoreOffers,
                                icon: _offersLoadingMore
                                    ? const SizedBox(
                                        width: 16,
                                        height: 16,
                                        child: CircularProgressIndicator(
                                          strokeWidth: 2,
                                        ),
                                      )
                                    : const Icon(Icons.expand_more),
                                label: Text(_offersLoadingMore
                                    ? 'Chargement...'
                                    : 'Charger plus'),
                              ),
                            ),
                          );
                        }
                        final product = displayProducts[index];
                        final productIdRaw = product['product_id'];
                        final productId =
                            productIdRaw is num ? productIdRaw.toInt() : 0;
                        final productName =
                            (product['product_name'] ?? '-').toString();
                        final requestedSourcePath =
                            (product['requested_source_attachment_path'] ?? '')
                                .toString()
                                .trim();
                        final requestedSourceFile =
                            (product['requested_source_attachment_file'] ?? '')
                                .toString()
                                .trim();
                        final requestedSourceIdRaw =
                            product['requested_source_attachment_id'];
                        final requestedSourceId = requestedSourceIdRaw is num
                            ? requestedSourceIdRaw.toInt()
                            : null;
                        final requestedSourceScore = _toDouble(
                            product['requested_source_attachment_score']);
                        final hasRequestedSource =
                            requestedSourcePath.isNotEmpty ||
                                requestedSourceId != null;
                        final requestedSourceLabel =
                            requestedSourceFile.isNotEmpty
                                ? requestedSourceFile
                                : (requestedSourceId == null
                                    ? ''
                                    : 'attachment #$requestedSourceId');
                        final requestedSourceLabelShort =
                            requestedSourceLabel.length > 42
                                ? '${requestedSourceLabel.substring(0, 42)}...'
                                : requestedSourceLabel;
                        final offers =
                            (product['offers'] as List<dynamic>?) ?? [];
                        final requestedQtyForProduct =
                            _toDouble(product['requested_qty']);
                        final visibleOfferCount =
                            _visibleOfferCount(productId, offers.length);
                        final visibleOffers =
                            offers.take(visibleOfferCount).toList();
                        final best = product['best_offer'];
                        final bestEqBasis = (best == null
                                ? ''
                                : (best['equivalence_basis'] ?? '')
                                    .toString()
                                    .trim())
                            .toLowerCase();
                        final bestIsNonConvertible =
                            bestEqBasis.startsWith('direct_');
                        final bestComparablePrice =
                            best == null ? null : _offerComparablePrice(best);
                        final bestTotalRequested =
                            (bestComparablePrice != null &&
                                    requestedQtyForProduct != null &&
                                    requestedQtyForProduct > 0)
                                ? (bestComparablePrice * requestedQtyForProduct)
                                : null;
                        final bestConformity =
                            _toDouble(product['best_conformity']);
                        final bestConformityOffer =
                            product['best_conformity_offer'];
                        final productTechCharRatio = bestConformityOffer == null
                            ? null
                            : _techComponentRatio(
                                bestConformityOffer, 'characteristics');
                        final productTechCharPercent =
                            bestConformityOffer == null
                                ? null
                                : _techComponentPercent(
                                    bestConformityOffer, 'characteristics');
                        final productTechReqRatio = bestConformityOffer == null
                            ? null
                            : _techComponentRatio(
                                bestConformityOffer, 'requirements');
                        final productTechReqPercent =
                            bestConformityOffer == null
                                ? null
                                : _techComponentPercent(
                                    bestConformityOffer, 'requirements');
                        final productTechAccRatio = bestConformityOffer == null
                            ? null
                            : _techComponentRatio(
                                bestConformityOffer, 'accessories');
                        final productTechAccPercent =
                            bestConformityOffer == null
                                ? null
                                : _techComponentPercent(
                                    bestConformityOffer, 'accessories');
                        final productTechDescPercent =
                            bestConformityOffer == null
                                ? null
                                : _techComponentPercent(
                                    bestConformityOffer, 'description');

                        return Container(
                          margin: const EdgeInsets.only(bottom: 12),
                          constraints: const BoxConstraints(minHeight: 230),
                          decoration: BoxDecoration(
                            color: Colors.white,
                            borderRadius: BorderRadius.circular(14),
                            border: Border.all(color: const Color(0xFFDCE3EC)),
                            boxShadow: const [
                              BoxShadow(
                                color: Color(0x12000000),
                                blurRadius: 8,
                                offset: Offset(0, 3),
                              ),
                            ],
                          ),
                          child: Padding(
                            padding: const EdgeInsets.all(12),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Row(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    const Icon(
                                      Icons.medical_information_outlined,
                                      color: Color(0xFF0B3D91),
                                    ),
                                    const SizedBox(width: 8),
                                    Expanded(
                                      child: Column(
                                        crossAxisAlignment:
                                            CrossAxisAlignment.start,
                                        children: [
                                          Text(
                                            productName,
                                            style: const TextStyle(
                                              fontSize: 15,
                                              fontWeight: FontWeight.w700,
                                              color: Color(0xFF0A1F44),
                                            ),
                                          ),
                                          const SizedBox(height: 6),
                                          Wrap(
                                            spacing: 8,
                                            runSpacing: 8,
                                            children: [
                                              Container(
                                                padding:
                                                    const EdgeInsets.symmetric(
                                                        horizontal: 10,
                                                        vertical: 5),
                                                decoration: BoxDecoration(
                                                  color:
                                                      const Color(0xFFE7EEF8),
                                                  borderRadius:
                                                      BorderRadius.circular(
                                                          999),
                                                ),
                                                child: Text(
                                                  '${offers.length} offre(s)',
                                                  style: const TextStyle(
                                                    fontSize: 12,
                                                    fontWeight: FontWeight.w600,
                                                    color: Color(0xFF274472),
                                                  ),
                                                ),
                                              ),
                                              if (offers.isNotEmpty)
                                                Container(
                                                  padding: const EdgeInsets
                                                      .symmetric(
                                                      horizontal: 10,
                                                      vertical: 5),
                                                  decoration: BoxDecoration(
                                                    color:
                                                        const Color(0xFFFFF7E6),
                                                    borderRadius:
                                                        BorderRadius.circular(
                                                            999),
                                                  ),
                                                  child: Text(
                                                    'Top $visibleOfferCount / ${offers.length} affichees',
                                                    style: const TextStyle(
                                                      fontSize: 12,
                                                      fontWeight:
                                                          FontWeight.w600,
                                                      color: Color(0xFF9A6700),
                                                    ),
                                                  ),
                                                ),
                                              if (hasRequestedSource)
                                                Container(
                                                  padding: const EdgeInsets
                                                      .symmetric(
                                                      horizontal: 10,
                                                      vertical: 5),
                                                  decoration: BoxDecoration(
                                                    color:
                                                        const Color(0xFFEFF6FF),
                                                    borderRadius:
                                                        BorderRadius.circular(
                                                            999),
                                                  ),
                                                  child: Text(
                                                    requestedSourceScore == null
                                                        ? 'Source PJ: $requestedSourceLabelShort'
                                                        : 'Source PJ: $requestedSourceLabelShort (${_fmtPercent(requestedSourceScore)})',
                                                    style: const TextStyle(
                                                      fontSize: 12,
                                                      fontWeight:
                                                          FontWeight.w600,
                                                      color: Color(0xFF1E3A8A),
                                                    ),
                                                  ),
                                                ),
                                              if (best != null)
                                                Container(
                                                  padding: const EdgeInsets
                                                      .symmetric(
                                                      horizontal: 10,
                                                      vertical: 5),
                                                  decoration: BoxDecoration(
                                                    color:
                                                        const Color(0xFF0F3D2E),
                                                    borderRadius:
                                                        BorderRadius.circular(
                                                            999),
                                                  ),
                                                  child: Text(
                                                    bestTotalRequested != null
                                                        ? 'Best (Total demandé): ${_fmtValue(bestTotalRequested, decimals: 2)} ${best['currency'] ?? ''}'
                                                        : 'Best (${bestIsNonConvertible ? 'PU' : 'PU compare'}): ${_fmtValue(bestComparablePrice, decimals: 2)} ${best['currency'] ?? ''}',
                                                    style: const TextStyle(
                                                      color: Colors.white,
                                                      fontWeight:
                                                          FontWeight.w600,
                                                      fontSize: 12,
                                                    ),
                                                  ),
                                                ),
                                              if (bestConformity != null)
                                                Container(
                                                  padding: const EdgeInsets
                                                      .symmetric(
                                                      horizontal: 10,
                                                      vertical: 5),
                                                  decoration: BoxDecoration(
                                                    color:
                                                        const Color(0xFF1D4ED8),
                                                    borderRadius:
                                                        BorderRadius.circular(
                                                            999),
                                                  ),
                                                  child: Text(
                                                    'Conformite max: ${_fmtPercent(bestConformity)}',
                                                    style: const TextStyle(
                                                      color: Colors.white,
                                                      fontWeight:
                                                          FontWeight.w600,
                                                      fontSize: 12,
                                                    ),
                                                  ),
                                                ),
                                            ],
                                          ),
                                          if (bestConformityOffer != null) ...[
                                            const SizedBox(height: 6),
                                            Theme(
                                              data: Theme.of(context).copyWith(
                                                dividerColor:
                                                    Colors.transparent,
                                              ),
                                              child: ExpansionTile(
                                                tilePadding: EdgeInsets.zero,
                                                childrenPadding:
                                                    EdgeInsets.zero,
                                                title: const Text(
                                                  'Resume conformite (meilleure offre)',
                                                  style: TextStyle(
                                                    fontSize: 12,
                                                    fontWeight: FontWeight.w700,
                                                    color: Color(0xFF334E68),
                                                  ),
                                                ),
                                                initiallyExpanded: false,
                                                children: [
                                                  Wrap(
                                                    spacing: 8,
                                                    runSpacing: 8,
                                                    children: [
                                                      Container(
                                                        padding:
                                                            const EdgeInsets
                                                                .symmetric(
                                                                horizontal: 10,
                                                                vertical: 5),
                                                        decoration:
                                                            BoxDecoration(
                                                          color: const Color(
                                                              0xFFEEF2FF),
                                                          borderRadius:
                                                              BorderRadius
                                                                  .circular(
                                                                      999),
                                                        ),
                                                        child: Text(
                                                          'Caract: ${_ratioWithPercent(productTechCharRatio, productTechCharPercent)}',
                                                          style:
                                                              const TextStyle(
                                                            fontSize: 12,
                                                            fontWeight:
                                                                FontWeight.w600,
                                                            color: Color(
                                                                0xFF1E3A8A),
                                                          ),
                                                        ),
                                                      ),
                                                      Container(
                                                        padding:
                                                            const EdgeInsets
                                                                .symmetric(
                                                                horizontal: 10,
                                                                vertical: 5),
                                                        decoration:
                                                            BoxDecoration(
                                                          color: const Color(
                                                              0xFFF5F3FF),
                                                          borderRadius:
                                                              BorderRadius
                                                                  .circular(
                                                                      999),
                                                        ),
                                                        child: Text(
                                                          'Exig: ${_ratioWithPercent(productTechReqRatio, productTechReqPercent)}',
                                                          style:
                                                              const TextStyle(
                                                            fontSize: 12,
                                                            fontWeight:
                                                                FontWeight.w600,
                                                            color: Color(
                                                                0xFF5B21B6),
                                                          ),
                                                        ),
                                                      ),
                                                      Container(
                                                        padding:
                                                            const EdgeInsets
                                                                .symmetric(
                                                                horizontal: 10,
                                                                vertical: 5),
                                                        decoration:
                                                            BoxDecoration(
                                                          color: const Color(
                                                              0xFFEFF6FF),
                                                          borderRadius:
                                                              BorderRadius
                                                                  .circular(
                                                                      999),
                                                        ),
                                                        child: Text(
                                                          'Acc: ${_ratioWithPercent(productTechAccRatio, productTechAccPercent)}',
                                                          style:
                                                              const TextStyle(
                                                            fontSize: 12,
                                                            fontWeight:
                                                                FontWeight.w600,
                                                            color: Color(
                                                                0xFF1D4ED8),
                                                          ),
                                                        ),
                                                      ),
                                                      Container(
                                                        padding:
                                                            const EdgeInsets
                                                                .symmetric(
                                                                horizontal: 10,
                                                                vertical: 5),
                                                        decoration:
                                                            BoxDecoration(
                                                          color: const Color(
                                                              0xFFECFDF5),
                                                          borderRadius:
                                                              BorderRadius
                                                                  .circular(
                                                                      999),
                                                        ),
                                                        child: Text(
                                                          'Desc: ${productTechDescPercent ?? '-'}',
                                                          style:
                                                              const TextStyle(
                                                            fontSize: 12,
                                                            fontWeight:
                                                                FontWeight.w600,
                                                            color: Color(
                                                                0xFF0F766E),
                                                          ),
                                                        ),
                                                      ),
                                                    ],
                                                  ),
                                                ],
                                              ),
                                            ),
                                          ],
                                          if (hasRequestedSource) ...[
                                            const SizedBox(height: 8),
                                            OutlinedButton.icon(
                                              onPressed: () =>
                                                  _openRequestedSourceAttachment(
                                                      product),
                                              icon: const Icon(
                                                  Icons.attach_file_outlined,
                                                  size: 16),
                                              label: Text(
                                                requestedSourceFile.isEmpty
                                                    ? 'Voir PJ source'
                                                    : 'Ouvrir PJ source',
                                              ),
                                            ),
                                          ],
                                        ],
                                      ),
                                    ),
                                  ],
                                ),
                                const SizedBox(height: 10),
                                Text(
                                  'Classement offres ($visibleOfferCount/${offers.length} affichees)',
                                  style: const TextStyle(
                                    fontWeight: FontWeight.w700,
                                    color: Color(0xFF102A43),
                                  ),
                                ),
                                const SizedBox(height: 8),
                                if (offers.isEmpty)
                                  const Text('Aucune offre')
                                else ...[
                                  ...visibleOffers.map((offer) =>
                                      _buildOfferCard(
                                          productId: productId,
                                          requestedName: productName,
                                          requestedQty: requestedQtyForProduct,
                                          offer: offer)),
                                  if (visibleOfferCount < offers.length)
                                    Padding(
                                      padding: const EdgeInsets.only(top: 8),
                                      child: Align(
                                        alignment: Alignment.centerLeft,
                                        child: OutlinedButton.icon(
                                          onPressed: () {
                                            setState(() {
                                              final next = visibleOfferCount +
                                                  _initialOffersVisible;
                                              _visibleOffersByProduct[
                                                      productId] =
                                                  next > offers.length
                                                      ? offers.length
                                                      : next;
                                            });
                                            _rememberVisibleOffersForCurrentCampaign();
                                          },
                                          icon: const Icon(Icons.expand_more,
                                              size: 16),
                                          label: Text(
                                              'Charger plus les offres (+${offers.length - visibleOfferCount}, $visibleOfferCount/${offers.length})'),
                                        ),
                                      ),
                                    ),
                                ],
                              ],
                            ),
                          ),
                        );
                      },
                    ),
                    if (_showBackToTop)
                      Positioned(
                        right: 16,
                        bottom: 16,
                        child: FloatingActionButton.small(
                          heroTag: 'results_back_to_top',
                          onPressed: () {
                            _productListController.animateTo(
                              0,
                              duration: const Duration(milliseconds: 300),
                              curve: Curves.easeOut,
                            );
                          },
                          child: const Icon(Icons.keyboard_arrow_up),
                        ),
                      ),
                  ],
                ),
        ),
      ],
    );
  }

  Widget _buildOfferCard({
    required int productId,
    required String requestedName,
    required double? requestedQty,
    required dynamic offer,
  }) {
    final supplier = (offer['supplier_name'] ?? '').toString();
    final phone = (offer['supplier_phone'] ?? '').toString();
    final extractedName = (offer['extracted_name'] ?? '').toString();
    final extractedDesc = (offer['extracted_description'] ?? '').toString();
    final gptMatchRate = _displayGptMatchScore(offer);
    final scoreSource = _displayMatchScoreSource(offer);
    final embMatchRate = _displayEmbeddingScore(offer);
    final techScore = _displayTechSimilarityScore(offer);
    final techStatusRaw = (offer['tech_sheet_similarity_display_status'] ??
            offer['tech_sheet_similarity_status'] ??
            '')
        .toString()
        .trim();
    final displayedConformity = techScore;
    final techCharRatio = _techComponentRatio(offer, 'characteristics');
    final techCharPercent = _techComponentPercent(offer, 'characteristics');
    final techReqRatio = _techComponentRatio(offer, 'requirements');
    final techReqPercent = _techComponentPercent(offer, 'requirements');
    final techAccRatio = _techComponentRatio(offer, 'accessories');
    final techAccPercent = _techComponentPercent(offer, 'accessories');
    final techDescPercent = _techComponentPercent(offer, 'description');
    final displayName =
        extractedName.trim().isEmpty ? requestedName : extractedName;
    final displayDesc = extractedDesc.trim();
    final requestedQtyResolved =
        requestedQty ?? _toDouble(offer['requested_qty']);
    final offeredQty = offer['offered_qty'];
    final price = _toDouble(offer['unit_price']);
    final equivalentPrice = _toDouble(offer['equivalent_unit_price']);
    final comparablePrice = _offerComparablePrice(offer);
    final eqBasis = (offer['equivalence_basis'] ?? '').toString().trim();
    final eqBasisShort = eqBasis.isEmpty
        ? ''
        : (eqBasis.length > 48 ? '${eqBasis.substring(0, 48)}…' : eqBasis);
    final eqBasisLower = eqBasis.toLowerCase();
    final isNonConvertible = eqBasisLower.startsWith('direct_');
    final totalRequestedComparable = (requestedQtyResolved != null &&
            (comparablePrice != null || (isNonConvertible && price != null)) &&
            requestedQtyResolved > 0)
        ? (requestedQtyResolved *
            (comparablePrice ?? (isNonConvertible ? price! : 0)))
        : null;
    final totalRequestedEstimated = (requestedQtyResolved != null &&
            price != null &&
            requestedQtyResolved > 0)
        ? (requestedQtyResolved * price)
        : null;
    final totalRequestedReason = totalRequestedComparable != null
        ? null
        : (requestedQtyResolved == null || requestedQtyResolved <= 0)
            ? 'qte demandee manquante'
            : (comparablePrice == null && !(isNonConvertible && price != null))
                ? (isNonConvertible ? 'PU manquant' : 'PU compare manquant')
                : 'non calculable';
    final eqFactor = _toDouble(offer['equivalence_factor']);
    final eqConfidence = _toDouble(offer['equivalence_confidence']);
    final currency = (offer['currency'] ?? '').toString();
    final rawPriceSource = (offer['price_source'] ?? '').toString().trim();
    final priceSource = _priceSourceLabel(rawPriceSource);
    final invoicePath = (offer['invoice_file_path'] ?? '').toString().trim();
    final invoiceFile = (offer['invoice_file_name'] ?? '').toString().trim();
    final invoiceIdRaw = offer['invoice_id'];
    final invoiceId = invoiceIdRaw is num ? invoiceIdRaw.toInt() : null;
    final techPath = (offer['tech_sheet_file_path'] ?? '').toString().trim();
    final techFile = (offer['tech_sheet_file'] ?? '').toString().trim();
    final techIdRaw = offer['tech_sheet_id'];
    final techId = techIdRaw is num ? techIdRaw.toInt() : null;
    final invoiceItemIdRaw = offer['invoice_item_id'];
    final detailOfferId =
        invoiceItemIdRaw is num ? invoiceItemIdRaw.toInt() : null;
    final detailLoading = detailOfferId != null &&
        _loadingTechDetailOfferIds.contains(detailOfferId);
    final hasTechDetails = _techBreakdownMap(offer) != null;
    final hasInvoice = invoicePath.isNotEmpty || invoiceId != null;
    final hasTech = techPath.isNotEmpty || techId != null;
    final hasAdvancedDetails = techStatusRaw.isNotEmpty ||
        techCharRatio != null ||
        techCharPercent != null ||
        techReqRatio != null ||
        techReqPercent != null ||
        techAccRatio != null ||
        techAccPercent != null ||
        techDescPercent != null ||
        priceSource.isNotEmpty ||
        (!isNonConvertible && equivalentPrice != null) ||
        (!isNonConvertible && eqFactor != null) ||
        (!isNonConvertible && eqConfidence != null) ||
        (!isNonConvertible && eqBasisShort.isNotEmpty);
    final tone =
        _scoreTone(displayedConformity ?? gptMatchRate ?? embMatchRate);

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(16),
      constraints: const BoxConstraints(minHeight: 220),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: const Color(0xFFD9E2EC), width: 1.2),
        boxShadow: const [
          BoxShadow(
            color: const Color(0x14000000),
            blurRadius: 14,
            offset: const Offset(0, 6),
          ),
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        const Icon(Icons.storefront_outlined,
                            size: 18, color: Color(0xFF334E68)),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            supplier.isEmpty ? 'Fournisseur inconnu' : supplier,
                            style: const TextStyle(
                              fontWeight: FontWeight.w700,
                              fontSize: 14,
                              color: Color(0xFF102A43),
                            ),
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      ],
                    ),
                    if (phone.isNotEmpty) ...[
                      const SizedBox(height: 4),
                      Text(
                        'Tel: $phone',
                        style: const TextStyle(
                            fontSize: 12, color: Color(0xFF627D98)),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ],
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          const Text(
            'Choix offert',
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w700,
              color: Color(0xFF475467),
            ),
          ),
          const SizedBox(height: 2),
          Text(
            displayName,
            style: const TextStyle(
              fontSize: 20,
              fontWeight: FontWeight.w800,
              color: Color(0xFF102A43),
            ),
          ),
          if (displayDesc.isNotEmpty) ...[
            const SizedBox(height: 3),
            Text(
              displayDesc,
              maxLines: 3,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 12, color: Color(0xFF486581)),
            ),
          ],
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              if (gptMatchRate != null)
                _buildInfoChip(
                  icon: Icons.smart_toy_outlined,
                  label: 'Score GPT',
                  value: _fmtPercent(gptMatchRate),
                  color: tone,
                ),
              _buildInfoChip(
                icon: Icons.verified_outlined,
                label: 'Conformite globale',
                value: displayedConformity == null
                    ? '-'
                    : _fmtPercent(displayedConformity),
                color: const Color(0xFF0F766E),
              ),
              if (gptMatchRate != null && scoreSource != null)
                _buildInfoChip(
                  icon: Icons.alt_route_outlined,
                  label: 'Source score',
                  value: scoreSource,
                  color: const Color(0xFF334E68),
                ),
            ],
          ),
          if (hasAdvancedDetails) ...[
            const SizedBox(height: 8),
            Theme(
              data: Theme.of(context).copyWith(
                dividerColor: Colors.transparent,
              ),
              child: ExpansionTile(
                tilePadding: EdgeInsets.zero,
                childrenPadding: EdgeInsets.zero,
                title: const Text(
                  'Details techniques',
                  style: TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                    color: Color(0xFF334E68),
                  ),
                ),
                initiallyExpanded: false,
                children: [
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      if (techStatusRaw.isNotEmpty)
                        _buildInfoChip(
                          icon: Icons.assignment_turned_in_outlined,
                          label: 'Statut fiche',
                          value: _techSimilarityStatusLabel(techStatusRaw),
                          color: const Color(0xFF4C6FFF),
                        ),
                      _buildInfoChip(
                        icon: Icons.checklist_rtl_outlined,
                        label: 'Caract. fiche',
                        value:
                            _ratioWithPercent(techCharRatio, techCharPercent),
                        color: const Color(0xFF4C6FFF),
                      ),
                      _buildInfoChip(
                        icon: Icons.assignment_outlined,
                        label: 'Exigences',
                        value: _ratioWithPercent(techReqRatio, techReqPercent),
                        color: const Color(0xFF7C3AED),
                      ),
                      _buildInfoChip(
                        icon: Icons.precision_manufacturing_outlined,
                        label: 'Accessoires',
                        value: _ratioWithPercent(techAccRatio, techAccPercent),
                        color: const Color(0xFF1D4ED8),
                      ),
                      _buildInfoChip(
                        icon: Icons.description_outlined,
                        label: 'Description',
                        value: techDescPercent ?? '-',
                        color: const Color(0xFF0F766E),
                      ),
                      if (priceSource.isNotEmpty)
                        _buildInfoChip(
                          icon: Icons.payments_outlined,
                          label: 'Source prix',
                          value: priceSource,
                          color: const Color(0xFF1B6CA8),
                        ),
                      if (!isNonConvertible && equivalentPrice != null)
                        _buildInfoChip(
                          icon: Icons.price_change_outlined,
                          label: 'PU eq.',
                          value:
                              '${_fmtValue(equivalentPrice, decimals: 2)} $currency',
                          color: const Color(0xFF0F766E),
                        ),
                      if (!isNonConvertible && eqFactor != null)
                        _buildInfoChip(
                          icon: Icons.exposure_plus_1_outlined,
                          label: 'Facteur eq.',
                          value: _fmtValue(eqFactor, decimals: 4),
                          color: const Color(0xFF0F766E),
                        ),
                      if (!isNonConvertible && eqConfidence != null)
                        _buildInfoChip(
                          icon: Icons.rule_folder_outlined,
                          label: 'Confiance eq.',
                          value: _fmtPercent(eqConfidence),
                          color: const Color(0xFF486581),
                        ),
                      if (!isNonConvertible && eqBasisShort.isNotEmpty)
                        _buildInfoChip(
                          icon: Icons.tune_outlined,
                          label: 'Base eq.',
                          value: eqBasisShort,
                          color: const Color(0xFF334E68),
                        ),
                    ],
                  ),
                ],
              ),
            ),
          ],
          if (hasInvoice ||
              hasTech ||
              hasTechDetails ||
              techStatusRaw.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                if (hasTechDetails || techStatusRaw.isNotEmpty)
                  OutlinedButton.icon(
                    onPressed: detailLoading
                        ? null
                        : () => _openTechSimilarityDetails(
                              requestedName: requestedName,
                              productId: productId,
                              offer: offer,
                            ),
                    icon: detailLoading
                        ? const SizedBox(
                            width: 16,
                            height: 16,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.policy_outlined, size: 16),
                    label: Text(
                        detailLoading ? 'Chargement...' : 'Details conformite'),
                  ),
                if (hasInvoice)
                  OutlinedButton.icon(
                    onPressed: () => _openInvoiceFile(offer),
                    icon: const Icon(Icons.picture_as_pdf_outlined, size: 16),
                    label:
                        Text(invoiceFile.isEmpty ? 'Voir facture' : 'Facture'),
                  ),
                if (hasTech)
                  OutlinedButton.icon(
                    onPressed: () => _openTechSheetFile(offer),
                    icon: const Icon(Icons.description_outlined, size: 16),
                    label: Text(
                        techFile.isEmpty ? 'Voir fiche' : 'Fiche technique'),
                  ),
              ],
            ),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 10,
            runSpacing: 10,
            children: [
              SizedBox(
                width: 175,
                child: _buildMetricBox(
                  label: 'Qte demandee',
                  value: _fmtValue(requestedQtyResolved, decimals: 2),
                  accent: tone,
                ),
              ),
              SizedBox(
                width: 175,
                child: _buildMetricBox(
                  label: 'Qte facture',
                  value: _fmtValue(offeredQty, decimals: 2),
                  accent: tone,
                ),
              ),
              SizedBox(
                width: 210,
                child: _buildMetricBox(
                  label: isNonConvertible ? 'PU' : 'PU compare',
                  value:
                      '${_fmtValue(comparablePrice ?? (isNonConvertible ? price : null), decimals: 2)} $currency',
                  accent: tone,
                ),
              ),
              if (price != null)
                SizedBox(
                  width: 210,
                  child: _buildMetricBox(
                    label: 'PU brut',
                    value: '${_fmtValue(price, decimals: 2)} $currency',
                    accent: tone,
                  ),
                ),
              SizedBox(
                width: 260,
                child: _buildMetricBox(
                  label: 'Total demande',
                  value: totalRequestedComparable != null
                      ? '${_fmtValue(totalRequestedComparable, decimals: 2)} $currency'
                      : (totalRequestedEstimated != null
                          ? '${_fmtValue(totalRequestedEstimated, decimals: 2)} $currency (estime, PU brut)'
                          : 'N/A (${totalRequestedReason ?? '-'})'),
                  accent: tone,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildInfoChip({
    required IconData icon,
    required String label,
    required String value,
    required Color color,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: color.withValues(alpha: 0.3)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 14, color: color),
          const SizedBox(width: 4),
          Text(
            '$label: $value',
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: color,
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildMetricBox({
    required String label,
    required String value,
    Color accent = const Color(0xFF486581),
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
      decoration: BoxDecoration(
        color: accent.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: accent.withValues(alpha: 0.18)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: accent.withValues(alpha: 0.90),
            ),
          ),
          const SizedBox(height: 6),
          Text(
            value,
            overflow: TextOverflow.ellipsis,
            maxLines: 2,
            style: const TextStyle(
              fontSize: 15,
              fontWeight: FontWeight.w800,
              color: Color(0xFF102A43),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildThresholdRow({
    required String label,
    required double value,
    required Color color,
    required void Function(double value) onChanged,
    double minValue = AppThresholds.minNormalized,
    double maxValue = AppThresholds.maxNormalized,
    bool enabled = true,
  }) {
    return Container(
      width: 235,
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.07),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withValues(alpha: 0.2)),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              '$label >=',
              style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 12),
              overflow: TextOverflow.ellipsis,
            ),
          ),
          IconButton(
            icon: const Icon(Icons.remove_circle_outline),
            color: enabled ? color : color.withValues(alpha: 0.45),
            iconSize: 18,
            visualDensity: VisualDensity.compact,
            onPressed: enabled
                ? () => onChanged((value - 0.01).clamp(minValue, maxValue))
                : null,
          ),
          SizedBox(
            width: 34,
            child: Text(
              value.toStringAsFixed(2),
              textAlign: TextAlign.center,
              style: TextStyle(
                  fontWeight: FontWeight.w700, color: color, fontSize: 12),
            ),
          ),
          IconButton(
            icon: const Icon(Icons.add_circle_outline),
            color: enabled ? color : color.withValues(alpha: 0.45),
            iconSize: 18,
            visualDensity: VisualDensity.compact,
            onPressed: enabled
                ? () => onChanged((value + 0.01).clamp(minValue, maxValue))
                : null,
          ),
        ],
      ),
    );
  }

  String _priceSourceLabel(String raw) {
    switch (raw.toLowerCase()) {
      case 'ocr_unit_price':
        return 'OCR (colonne PU)';
      case 'derived_amount_div_qty':
        return 'Calcule: montant/qte';
      case 'amount_only':
        return 'Montant (fallback)';
      case 'local_table':
        return 'Parser local tableau';
      case 'manual_correction':
        return 'Correction manuelle';
      case 'supplier_form':
        return 'Saisie fournisseur';
      case 'document_ai':
        return 'OCR';
      default:
        return raw.isEmpty ? '' : raw;
    }
  }

  Color _scoreTone(double? score) {
    if (score == null) return const Color(0xFF486581);
    if (score >= _embedConfirm) return const Color(0xFF0F766E);
    if (score >= _embedVerify) return const Color(0xFFD97706);
    return const Color(0xFF6B7280);
  }

  Widget _buildStatistics() {
    final responsesSet =
        _responses.map<int>((r) => r['supplier_id'] as int).toSet();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (_stats != null)
          Wrap(
            spacing: 16,
            runSpacing: 16,
            children: [
              _statCard(
                  'Total recipients', _stats!['total_recipients'].toString()),
              _statCard('Sent', _stats!['sent_count'].toString()),
              _statCard('Failed', _stats!['failed_count'].toString()),
              _statCard(
                  'Suppliers responded', _stats!['responses_count'].toString()),
              _statCard('Pending responses',
                  _stats!['pending_responses_count'].toString()),
            ],
          ),
        const SizedBox(height: 16),
        const Text('Suppliers status',
            style: TextStyle(fontWeight: FontWeight.bold)),
        const SizedBox(height: 8),
        Expanded(
          child: SingleChildScrollView(
            child: DataTable(
              columns: const [
                DataColumn(label: Text('Supplier')),
                DataColumn(label: Text('Delivery status')),
                DataColumn(label: Text('Responded')),
              ],
              rows: _recipients.map((recipient) {
                final supplierId = recipient['supplier_id'] as int;
                final supplierName =
                    _supplierNames[supplierId] ?? 'Supplier $supplierId';
                final responded = responsesSet.contains(supplierId);
                return DataRow(cells: [
                  DataCell(Text(supplierName)),
                  DataCell(Text(recipient['delivery_status'] as String)),
                  DataCell(Text(responded ? 'Yes' : 'No')),
                ]);
              }).toList(),
            ),
          ),
        ),
      ],
    );
  }

  Widget _statCard(String label, String value) {
    return Card(
      child: SizedBox(
        width: 180,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(label,
                  style: const TextStyle(fontSize: 12, color: Colors.grey)),
              const SizedBox(height: 8),
              Text(value,
                  style: const TextStyle(
                      fontSize: 20, fontWeight: FontWeight.bold)),
            ],
          ),
        ),
      ),
    );
  }
}
