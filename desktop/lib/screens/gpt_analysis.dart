import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';

import '../api/campaigns_api.dart';
import '../api/client.dart';
import '../utils/file_opening.dart';
import '../widgets/empty_state.dart';

class GptAnalysisScreen extends StatefulWidget {
  const GptAnalysisScreen({super.key});

  @override
  State<GptAnalysisScreen> createState() => _GptAnalysisScreenState();
}

class _GptAnalysisScreenState extends State<GptAnalysisScreen> {
  static const int _pageSize = 100;

  bool _loadingCampaigns = true;
  bool _loadingRows = false;
  String? _error;
  List<dynamic> _campaigns = [];
  int? _selectedCampaignId;
  List<dynamic> _rows = [];
  int _rowsRequestId = 0;
  Timer? _countdownTicker;
  final Set<int> _loadingDetailOfferIds = <int>{};
  final TextEditingController _searchController = TextEditingController();
  String _query = '';

  String _campaignDisplayLabel(dynamic raw) {
    if (raw is! Map) return '-';
    final id = raw['id'] is num ? (raw['id'] as num).toInt() : 0;
    final title = (raw['title'] ?? '').toString().trim();
    final establishment = (raw['establishment_name'] ?? '').toString().trim();
    final baseTitle = title.isEmpty ? 'CAMP $id' : title;
    if (establishment.isEmpty) return baseTitle;
    return '$baseTitle - $establishment';
  }

  void _startCountdownTicker() {
    _countdownTicker?.cancel();
    _countdownTicker = Timer.periodic(const Duration(minutes: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  String _campaignCountdown(dynamic raw) {
    final value = DateTime.tryParse((raw ?? '').toString());
    if (value == null) return 'Date depot non renseignee';
    final now = DateTime.now();
    final target = DateTime(value.year, value.month, value.day, 23, 59, 59);
    final diff = target.difference(now);
    if (diff.isNegative) {
      final late = diff.abs();
      return 'Depasse de ${late.inDays}j ${late.inHours.remainder(24)}h ${late.inMinutes.remainder(60)}m';
    }
    return 'Dans ${diff.inDays}j ${diff.inHours.remainder(24)}h ${diff.inMinutes.remainder(60)}m';
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
    final target = DateTime(value.year, value.month, value.day, 23, 59, 59);
    final diff = target.difference(now);
    if (diff.isNegative || diff.inDays <= 1) {
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
    if (diff.inDays <= 5) {
      return (
        background: const Color(0xFFFFF8D6),
        border: const Color(0xFFE7C85B),
        text: const Color(0xFF8A6D00),
      );
    }
    return (
      background: const Color(0xFFE8F5E9),
      border: const Color(0xFF9AD1A4),
      text: const Color(0xFF1B4332),
    );
  }

  Widget _campaignCountdownBadge(dynamic raw) {
    final colors = _campaignCountdownColors(raw);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
      decoration: BoxDecoration(
        color: colors.background,
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: colors.border),
      ),
      child: Text(
        _campaignCountdown(raw),
        overflow: TextOverflow.ellipsis,
        style: TextStyle(
          fontSize: 11,
          fontWeight: FontWeight.w700,
          color: colors.text,
        ),
      ),
    );
  }

  @override
  void initState() {
    super.initState();
    _startCountdownTicker();
    _searchController.addListener(() {
      if (!mounted) return;
      setState(() => _query = _searchController.text.trim().toLowerCase());
    });
    unawaited(_loadCampaigns());
  }

  @override
  void dispose() {
    _countdownTicker?.cancel();
    _searchController.dispose();
    super.dispose();
  }

  void _showError(Object err) {
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
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

  Widget _campaignSelector() {
    final selectedCampaign = _selectedCampaignMap();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        ConstrainedBox(
          constraints: const BoxConstraints(minWidth: 240, maxWidth: 460),
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 10),
            decoration: BoxDecoration(
              color: Colors.white,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: const Color(0xFFCFD8E3)),
            ),
            child: DropdownButtonHideUnderline(
              child: DropdownButton<int>(
                value: _selectedCampaignId,
                isExpanded: true,
                items: _campaigns
                    .map(
                      (c) => DropdownMenuItem<int>(
                        value: c['id'] as int,
                        child: Text(
                          _campaignDisplayLabel(c),
                          overflow: TextOverflow.ellipsis,
                          maxLines: 1,
                        ),
                      ),
                    )
                    .toList(),
                onChanged: (value) async {
                  if (value == null) return;
                  setState(() => _selectedCampaignId = value);
                  await _loadRows();
                },
              ),
            ),
          ),
        ),
        if (selectedCampaign != null) ...[
          const SizedBox(height: 8),
          ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 320),
            child: _campaignCountdownBadge(selectedCampaign['deposit_date']),
          ),
        ],
      ],
    );
  }

  String _ensureFileExtension(String path, String extension) {
    final suffix = '.${extension.toLowerCase()}';
    if (path.toLowerCase().endsWith(suffix)) return path;
    return '$path$suffix';
  }

  double? _toDouble(dynamic value) {
    if (value is num) return value.toDouble();
    if (value is String) return double.tryParse(value.trim().replaceAll(',', '.'));
    return null;
  }

  String _fmtPercent(double value) => '${(value * 100).toStringAsFixed(0)}%';

  String _fmtValue(dynamic value, {int decimals = 2}) {
    final number = _toDouble(value);
    if (number == null) return '-';
    return number.toStringAsFixed(decimals);
  }

  String _fmtMoney(dynamic value, {String currency = ''}) {
    final number = _toDouble(value);
    if (number == null) return '-';
    final raw = number == number.roundToDouble()
        ? number.toInt().toString()
        : number.toStringAsFixed(2);
    final normalized = raw.replaceAll('.', ',');
    final suffix = currency.trim().isEmpty ? '' : ' ${currency.trim()}';
    return '$normalized$suffix';
  }

  Color _scoreTone(double? score) {
    if (score == null) return const Color(0xFF64748B);
    if (score >= 0.85) return const Color(0xFF0F766E);
    if (score >= 0.70) return const Color(0xFF2563EB);
    if (score >= 0.50) return const Color(0xFFB45309);
    return const Color(0xFFB42318);
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

  String _gptDecisionLabel(String decision) {
    switch (decision.toLowerCase()) {
      case 'strong_match':
        return 'Correspondance forte';
      case 'close_match':
        return 'Correspondance proche';
      case 'partial_match':
        return 'Correspondance partielle';
      case 'weak_match':
        return 'Correspondance faible';
      case 'no_match':
        return 'Aucune correspondance';
      case 'comparable':
        return 'Comparable';
      case 'not_comparable':
        return 'Non comparable';
      default:
        return decision;
    }
  }

  Map<String, dynamic>? _techBreakdownMap(dynamic offer) {
    final raw = offer['tech_sheet_similarity_breakdown'];
    if (raw is Map) return Map<String, dynamic>.from(raw);
    return null;
  }

  Map<String, dynamic>? _techComponent(dynamic offer, String key) {
    final breakdown = _techBreakdownMap(offer);
    if (breakdown == null) return null;
    final components = breakdown['components'];
    if (components is! Map) return null;
    final raw = components[key];
    if (raw is! Map) return null;
    return Map<String, dynamic>.from(raw);
  }

  double? _displayTechSimilarityScore(dynamic offer) {
    final display = _toDouble(offer['tech_sheet_similarity_display_score']);
    return display ?? _toDouble(offer['tech_sheet_similarity_score']);
  }

  String _ratioWithPercent(String? ratio, String? percent) {
    if (ratio != null && ratio.isNotEmpty && percent != null && percent.isNotEmpty) {
      return '$ratio ($percent)';
    }
    if (ratio != null && ratio.isNotEmpty) return ratio;
    if (percent != null && percent.isNotEmpty) return percent;
    return '-';
  }

  String? _techComponentRatio(dynamic offer, String key) {
    final comp = _techComponent(offer, key);
    if (comp == null) return null;
    final matched = comp['matched'];
    final total = comp['total'];
    if (matched is num && total is num) {
      return '${matched.toInt()} / ${total.toInt()}';
    }
    return null;
  }

  String? _techComponentPercent(dynamic offer, String key) {
    final comp = _techComponent(offer, key);
    if (comp == null) return null;
    final score = _toDouble(comp['coverage']) ?? _toDouble(comp['score']);
    if (score == null) return null;
    return _fmtPercent(score.clamp(0, 1));
  }

  Future<bool> _tryOpenLocalFile(String path) async => openWithShell(path);

  bool _isLocalWindowsPath(String value) => isLocalWindowsPath(value);

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
    await openWithShell(path);
  }

  Future<void> _openInvoiceFile(dynamic offer) async {
    final invoicePath = (offer['invoice_file_path'] ?? '').toString().trim();
    final invoiceFile = (offer['invoice_file_name'] ?? '').toString().trim();
    final invoiceIdRaw = offer['invoice_id'];
    final invoiceId = invoiceIdRaw is num ? invoiceIdRaw.toInt() : null;
    if (invoicePath.isNotEmpty &&
        (_isLocalWindowsPath(invoicePath) || invoicePath.startsWith('http'))) {
      final opened = await _tryOpenLocalFile(invoicePath);
      if (opened) return;
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
        if (mounted) _showError(err);
      }
    }
  }

  Future<void> _openTechSheetFile(dynamic offer) async {
    final techPath = (offer['tech_sheet_file_path'] ?? '').toString().trim();
    final techFile = (offer['tech_sheet_file'] ?? '').toString().trim();
    final techIdRaw = offer['tech_sheet_id'];
    final techId = techIdRaw is num ? techIdRaw.toInt() : null;
    if (techPath.isNotEmpty &&
        (_isLocalWindowsPath(techPath) || techPath.startsWith('http'))) {
      final opened = await _tryOpenLocalFile(techPath);
      if (opened) return;
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
        if (mounted) _showError(err);
      }
    }
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
        (_isLocalWindowsPath(sourcePath) || sourcePath.startsWith('http'))) {
      final opened = await _tryOpenLocalFile(sourcePath);
      if (opened) return;
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
        if (mounted) _showError(err);
        return;
      }
    }

    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('PJ source introuvable')),
    );
  }

  Future<void> _loadCampaigns() async {
    setState(() {
      _loadingCampaigns = true;
      _error = null;
    });
    try {
      final rawCampaigns = await CampaignsApi.list();
      final campaigns = rawCampaigns.where((raw) {
        if (raw is! Map) return false;
        final mode = (raw['processing_mode'] ?? '')
            .toString()
            .trim()
            .toLowerCase();
        return mode == 'standard';
      }).toList()
        ..sort((a, b) {
          final aId = a is Map && a['id'] is num ? (a['id'] as num).toInt() : 0;
          final bId = b is Map && b['id'] is num ? (b['id'] as num).toInt() : 0;
          return bId.compareTo(aId);
        });
      int? selected = _selectedCampaignId;
      if (campaigns.isNotEmpty) {
        final exists = campaigns.any((raw) => raw is Map && raw['id'] == selected);
        if (!exists) {
          final firstId = campaigns.first['id'];
          selected = firstId is num ? firstId.toInt() : null;
        }
      } else {
        selected = null;
      }
      if (!mounted) return;
      setState(() {
        _campaigns = campaigns;
        _selectedCampaignId = selected;
        _loadingCampaigns = false;
      });
      if (selected != null) await _loadRows();
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = ApiClient.formatError(err);
        _loadingCampaigns = false;
      });
    }
  }

  Future<void> _loadRows() async {
    final campaignId = _selectedCampaignId;
    if (campaignId == null) {
      setState(() => _rows = []);
      return;
    }
    final requestId = ++_rowsRequestId;
    setState(() {
      _loadingRows = true;
      _rows = [];
      _error = null;
    });
    try {
      final allRows = <dynamic>[];
      var page = 1;
      var hasMore = true;
      while (hasMore && page <= 200) {
        final payload = await CampaignsApi.offersByProduct(
          campaignId,
          conformOnly: false,
          page: page,
          pageSize: _pageSize,
        );
        if (!mounted || requestId != _rowsRequestId) return;
        allRows.addAll(List<dynamic>.from((payload['items'] as List?) ?? const []));
        hasMore = payload['has_more'] == true;
        page += 1;
      }
      if (!mounted || requestId != _rowsRequestId) return;
      setState(() => _rows = allRows);
    } catch (err) {
      if (!mounted || requestId != _rowsRequestId) return;
      setState(() => _error = ApiClient.formatError(err));
    } finally {
      if (mounted && requestId == _rowsRequestId) {
        setState(() => _loadingRows = false);
      }
    }
  }

  Future<void> _saveExport({
    required List<int> bytes,
    required String fileName,
  }) async {
    final path = await FilePicker.platform.saveFile(
      dialogTitle: 'Enregistrer l export Excel',
      fileName: fileName,
      type: FileType.custom,
      allowedExtensions: const ['xlsx'],
    );
    if (path == null) return;
    final savePath = _ensureFileExtension(path, 'xlsx');
    await File(savePath).writeAsBytes(bytes, flush: true);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text('Export enregistre: $savePath')),
    );
  }

  Future<void> _exportBestComplianceXlsx() async {
    final campaignId = _selectedCampaignId;
    if (campaignId == null) return;
    try {
      final bytes = await CampaignsApi.exportGptBestComplianceXlsx(campaignId);
      final campaign = _campaigns.cast<Map?>().firstWhere(
            (c) => c?['id'] == campaignId,
            orElse: () => null,
          );
      final title = (campaign?['title'] ?? 'campaign_$campaignId').toString();
      await _saveExport(
        bytes: bytes,
        fileName: '${title.replaceAll(' ', '_')}_gpt_meilleure_conformite.xlsx',
      );
    } catch (err) {
      if (mounted) _showError(err);
    }
  }

  Future<void> _exportLowestPriceXlsx() async {
    final campaignId = _selectedCampaignId;
    if (campaignId == null) return;
    try {
      final bytes = await CampaignsApi.exportGptLowestPriceXlsx(campaignId);
      final campaign = _campaigns.cast<Map?>().firstWhere(
            (c) => c?['id'] == campaignId,
            orElse: () => null,
          );
      final title = (campaign?['title'] ?? 'campaign_$campaignId').toString();
      await _saveExport(
        bytes: bytes,
        fileName: '${title.replaceAll(' ', '_')}_gpt_moins_cher.xlsx',
      );
    } catch (err) {
      if (mounted) _showError(err);
    }
  }

  Future<void> _openTechSimilarityDetails({
    required String requestedName,
    required int productId,
    required dynamic offer,
  }) async {
    final invoiceItemIdRaw = offer['invoice_item_id'];
    final detailOfferId = invoiceItemIdRaw is num ? invoiceItemIdRaw.toInt() : null;
    if (detailOfferId != null && detailOfferId > 0) {
      if (_loadingDetailOfferIds.contains(detailOfferId)) return;
      if (mounted) setState(() => _loadingDetailOfferIds.add(detailOfferId));
    }
    dynamic effectiveOffer = offer;
    final hasBreakdown = _techBreakdownMap(offer) != null;
    final campaignId = _selectedCampaignId;
    try {
      if (!hasBreakdown && campaignId != null && invoiceItemIdRaw is num && invoiceItemIdRaw.toInt() > 0) {
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
      await _showTechSimilarityDialog(requestedName: requestedName, offer: effectiveOffer);
    } catch (err) {
      if (mounted) _showError(err);
    } finally {
      if (detailOfferId != null && detailOfferId > 0 && mounted) {
        setState(() => _loadingDetailOfferIds.remove(detailOfferId));
      }
    }
  }

  Future<void> _showTechSimilarityDialog({
    required String requestedName,
    required dynamic offer,
  }) async {
    final breakdown = _techBreakdownMap(offer) ?? const {};
    final componentsRaw = breakdown['components'];
    final components = componentsRaw is Map ? componentsRaw : const {};
    final gptDecision = _gptDecisionLabel(
      (breakdown['gpt_compare_decision'] ?? '').toString().trim(),
    );
    final gptSummary = (breakdown['gpt_compare_reasoning_summary'] ?? breakdown['gpt_reason'] ?? '').toString().trim();
    final gptComparable = breakdown['gpt_compare_comparable'] == true;
    final scoreRaw = _displayTechSimilarityScore(offer);
    final score = scoreRaw == null ? '-' : _fmtPercent(scoreRaw);
    final status = _techSimilarityStatusLabel((offer['tech_sheet_similarity_display_status'] ?? offer['tech_sheet_similarity_status'] ?? '').toString());

    List<String> lines(dynamic raw) {
      if (raw is! List) return const [];
      return raw.map((e) => e.toString().trim()).where((e) => e.isNotEmpty).toList();
    }

    Widget pill(String label, String value, Color color) => Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
          decoration: BoxDecoration(
            color: color.withValues(alpha: 0.10),
            borderRadius: BorderRadius.circular(999),
            border: Border.all(color: color.withValues(alpha: 0.28)),
          ),
          child: Text(
            '$label: $value',
            style: TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: color),
          ),
        );

    Widget bulletBlock(String title, List<String> values, Color color) => Container(
          width: double.infinity,
          margin: const EdgeInsets.only(bottom: 10),
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: color.withValues(alpha: 0.07),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: color.withValues(alpha: 0.22)),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title, style: TextStyle(fontWeight: FontWeight.w700, color: color)),
              const SizedBox(height: 8),
              if (values.isEmpty)
                const Text('Aucun element', style: TextStyle(fontSize: 12, color: Color(0xFF667085)))
              else
                ...values.take(12).map((row) => Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Text('- $row', style: const TextStyle(fontSize: 12, color: Color(0xFF102A43))),
                    )),
            ],
          ),
        );

    Widget componentRow(String key, String title) {
      final raw = components[key];
      final map = raw is Map ? Map<String, dynamic>.from(raw) : <String, dynamic>{};
      final scoreValue = _toDouble(map['score']);
      final matched = map['matched'];
      final total = map['total'];
      final ratio = (matched is num && total is num) ? '${matched.toInt()} / ${total.toInt()}' : '-';
      return Container(
        margin: const EdgeInsets.only(bottom: 10),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: Colors.white,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: const Color(0xFFD9E2EC)),
        ),
        child: Row(
          children: [
            Expanded(
              child: Text(title, style: const TextStyle(fontWeight: FontWeight.w700, color: Color(0xFF102A43))),
            ),
            pill('Couverture', ratio, const Color(0xFF1D4ED8)),
            const SizedBox(width: 8),
            pill('Score', scoreValue == null ? '-' : _fmtPercent(scoreValue), const Color(0xFF0F766E)),
          ],
        ),
      );
    }

    final debugPayload = <String, dynamic>{
      'statut': status,
      'score_affiche': offer['tech_sheet_similarity_display_score'],
      'score_brut': offer['tech_sheet_similarity_score'],
      'details': breakdown,
    };
    final debugPreview = const JsonEncoder.withIndent('  ').convert(debugPayload);

    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Analyse'),
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
                      Text('Produit demande : $requestedName', style: const TextStyle(fontWeight: FontWeight.w700, color: Color(0xFF102A43))),
                      const SizedBox(height: 8),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          pill('Comparabilite', gptComparable ? 'Oui' : 'Non', gptComparable ? const Color(0xFF0F766E) : const Color(0xFFB42318)),
                          if (gptDecision.isNotEmpty) pill('Decision', gptDecision, const Color(0xFF7C3AED)),
                          pill('Statut', status, const Color(0xFF1D4ED8)),
                          pill('Conformite globale', score, const Color(0xFF0F766E)),
                        ],
                      ),
                      if (gptSummary.isNotEmpty) ...[
                        const SizedBox(height: 10),
                        Text(gptSummary, style: const TextStyle(color: Color(0xFF334E68), height: 1.35)),
                      ],
                    ],
                  ),
                ),
                const SizedBox(height: 10),
                bulletBlock('Points forts', lines(breakdown['gpt_compare_strong_matches']), const Color(0xFF0F766E)),
                bulletBlock('Differences relevees', lines(breakdown['gpt_compare_differences']), const Color(0xFFB54708)),
                bulletBlock('Exigences manquantes', lines(breakdown['gpt_compare_missing_requirements']), const Color(0xFFB42318)),
                bulletBlock('Accessoires manquants', lines(breakdown['gpt_compare_missing_accessories']), const Color(0xFF1D4ED8)),
                componentRow('characteristics', 'Caracteristiques'),
                componentRow('requirements', 'Exigences'),
                componentRow('accessories', 'Accessoires'),
                componentRow('description', 'Description'),
                Theme(
                  data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
                  child: ExpansionTile(
                    tilePadding: EdgeInsets.zero,
                    childrenPadding: EdgeInsets.zero,
                    title: const Text('Technique (debogage)'),
                    subtitle: const Text('JSON et metadonnees'),
                    children: [
                      Container(
                        width: double.infinity,
                        padding: const EdgeInsets.all(10),
                        decoration: BoxDecoration(
                          color: const Color(0xFFF8FAFC),
                          borderRadius: BorderRadius.circular(8),
                          border: Border.all(color: const Color(0xFFD9E2EC)),
                        ),
                        child: SelectableText(debugPreview, style: const TextStyle(fontSize: 12, color: Color(0xFF334E68), fontFamily: 'monospace')),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
        actions: [TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Fermer'))],
      ),
    );
  }

  List<Map<String, dynamic>> _displayRows() {
    final out = <Map<String, dynamic>>[];
    for (final raw in _rows) {
      if (raw is! Map) continue;
      final row = Map<String, dynamic>.from(raw);
      final productName = (row['product_name'] ?? '').toString();
      if (_query.isNotEmpty && !productName.toLowerCase().contains(_query)) continue;
      final offers = List<Map<String, dynamic>>.from(
        ((row['offers'] as List?) ?? const []).whereType<Map>().map((e) => Map<String, dynamic>.from(e)),
      );
      offers.sort((a, b) => (_displayTechSimilarityScore(b) ?? -1.0).compareTo(_displayTechSimilarityScore(a) ?? -1.0));
      row['offers'] = offers;
      row['best_offer'] = offers.isEmpty ? null : offers.first;
      row['best_score'] = offers.isEmpty ? null : _displayTechSimilarityScore(offers.first);
      out.add(row);
    }
    out.sort((a, b) {
      final aScore = _toDouble(a['best_score']);
      final bScore = _toDouble(b['best_score']);
      if (aScore == null && bScore == null) {
        return (a['product_name'] ?? '').toString().compareTo((b['product_name'] ?? '').toString());
      }
      if (aScore == null) return 1;
      if (bScore == null) return -1;
      return bScore.compareTo(aScore);
    });
    return out;
  }
  Widget _pill(String label, String value, Color color) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.10),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: color.withValues(alpha: 0.28)),
      ),
      child: Text(
        '$label: $value',
        style: TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: color),
      ),
    );
  }

  Widget _offerCard({
    required int productId,
    required String requestedName,
    required dynamic requestedQty,
    required dynamic offer,
  }) {
    final supplier = (offer['supplier_name'] ?? '').toString().trim();
    final phone = (offer['supplier_phone'] ?? '').toString().trim();
    final offeredName = (offer['extracted_name'] ?? '-').toString().trim();
    final score = _displayTechSimilarityScore(offer);
    final status = _techSimilarityStatusLabel((offer['tech_sheet_similarity_display_status'] ?? offer['tech_sheet_similarity_status'] ?? '-').toString());
    final accent = _scoreTone(score);
    final invoiceItemIdRaw = offer['invoice_item_id'];
    final detailOfferId = invoiceItemIdRaw is num ? invoiceItemIdRaw.toInt() : null;
    final detailLoading = detailOfferId != null && _loadingDetailOfferIds.contains(detailOfferId);
    final hasTechDetails = _techBreakdownMap(offer) != null || (offer['tech_sheet_similarity_status'] ?? '').toString().trim().isNotEmpty;
    final hasInvoice = (offer['invoice_file_path'] ?? '').toString().trim().isNotEmpty || offer['invoice_id'] is num;
    final hasTech = (offer['tech_sheet_file_path'] ?? '').toString().trim().isNotEmpty || offer['tech_sheet_id'] is num;
    final currency = (offer['currency'] ?? '').toString().trim();
    final unitPriceValue = _toDouble(offer['unit_price']);
    final requestedQtyValue = _toDouble(requestedQty);
    final totalValue = unitPriceValue != null && requestedQtyValue != null
        ? unitPriceValue * requestedQtyValue
        : null;

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: accent.withValues(alpha: 0.28)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(offeredName.isEmpty ? requestedName : offeredName, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w800, color: Color(0xFF101828))),
          const SizedBox(height: 6),
          Text(supplier.isEmpty ? 'Fournisseur inconnu' : supplier, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: Color(0xFF334E68))),
          if (phone.isNotEmpty) ...[
            const SizedBox(height: 3),
            Text('Telephone : $phone', style: const TextStyle(fontSize: 12, color: Color(0xFF667085))),
          ],
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _pill('Score', score == null ? '-' : _fmtPercent(score), accent),
              _pill('Statut', status, const Color(0xFF1D4ED8)),
              _pill(
                'Prix unitaire',
                _fmtMoney(unitPriceValue, currency: currency),
                const Color(0xFF0F766E),
              ),
              _pill(
                'Total quantite demandee',
                _fmtMoney(totalValue, currency: currency),
                const Color(0xFFB45309),
              ),
              _pill('Caract.', _ratioWithPercent(_techComponentRatio(offer, 'characteristics'), _techComponentPercent(offer, 'characteristics')), const Color(0xFF4C6FFF)),
              _pill('Exigences', _ratioWithPercent(_techComponentRatio(offer, 'requirements'), _techComponentPercent(offer, 'requirements')), const Color(0xFF7C3AED)),
              _pill('Accessoires', _ratioWithPercent(_techComponentRatio(offer, 'accessories'), _techComponentPercent(offer, 'accessories')), const Color(0xFF1D4ED8)),
            ],
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              if (hasTechDetails)
                OutlinedButton.icon(
                  onPressed: detailLoading ? null : () => _openTechSimilarityDetails(requestedName: requestedName, productId: productId, offer: offer),
                  icon: detailLoading
                      ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.smart_toy_outlined, size: 16),
                  label: Text(detailLoading ? 'Chargement...' : 'Voir analyse'),
                ),
              if (hasInvoice)
                OutlinedButton.icon(
                  onPressed: () => _openInvoiceFile(offer),
                  icon: const Icon(Icons.picture_as_pdf_outlined, size: 16),
                  label: const Text('Voir facture'),
                ),
              if (hasTech)
                OutlinedButton.icon(
                  onPressed: () => _openTechSheetFile(offer),
                  icon: const Icon(Icons.description_outlined, size: 16),
                  label: const Text('Voir fiche'),
                ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildProductCard(Map<String, dynamic> row, int rank) {
    final productIdRaw = row['product_id'];
    final productId = productIdRaw is num ? productIdRaw.toInt() : 0;
    final productName = (row['product_name'] ?? '-').toString();
    final offers = List<Map<String, dynamic>>.from((row['offers'] as List?) ?? const []);
    final best = row['best_offer'];
    final bestScore = _toDouble(row['best_score']);
    final accent = _scoreTone(bestScore);
    final hasRequestedSource =
        (row['requested_source_attachment_path'] ?? '').toString().trim().isNotEmpty ||
        row['requested_source_attachment_id'] is num;

    return Container(
      margin: const EdgeInsets.only(bottom: 14),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: const Color(0xFFDCE3EC)),
        boxShadow: const [BoxShadow(color: Color(0x12000000), blurRadius: 8, offset: Offset(0, 3))],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                width: 34,
                height: 34,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: accent.withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Text('$rank', style: TextStyle(fontWeight: FontWeight.w800, color: accent)),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(productName, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w800, color: Color(0xFF0A1F44))),
                    const SizedBox(height: 8),
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        _pill('Score max', bestScore == null ? '-' : _fmtPercent(bestScore), accent),
                        _pill('Offres', offers.length.toString(), const Color(0xFF274472)),
                        _pill('Quantite demandee', _fmtValue(row['requested_qty']), const Color(0xFF0B3D91)),
                      ],
                    ),
                  ],
                ),
              ),
            ],
          ),
          if (best != null) ...[
            const SizedBox(height: 10),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton.icon(
                  onPressed: () => _openTechSimilarityDetails(requestedName: productName, productId: productId, offer: best),
                  icon: const Icon(Icons.smart_toy_outlined, size: 16),
                  label: const Text('Voir analyse'),
                ),
                if (hasRequestedSource)
                  OutlinedButton.icon(
                    onPressed: () => _openRequestedSourceAttachment(row),
                    icon: const Icon(Icons.attach_file_outlined, size: 16),
                    label: const Text('Ouvrir PJ source'),
                  ),
              ],
            ),
          ],
          const SizedBox(height: 10),
          if (offers.isEmpty)
            const Text('Aucune offre', style: TextStyle(fontSize: 13, color: Color(0xFF667085)))
          else
            ...offers.map((offer) => _offerCard(
                  productId: productId,
                  requestedName: productName,
                  requestedQty: row['requested_qty'],
                  offer: offer,
                )),
        ],
      ),
    );
  }

  Widget _buildHeader() {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          colors: [Color(0xFFF4F8FF), Color(0xFFFFFFFF)],
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
        ),
        border: Border.all(color: const Color(0xFFD7E5FF)),
        borderRadius: BorderRadius.circular(16),
      ),
      child: Wrap(
        spacing: 12,
        runSpacing: 10,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          const Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.smart_toy_outlined, color: Color(0xFF0B3D91)),
              SizedBox(width: 8),
              Text('Analyses', style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold, color: Color(0xFF0B2545))),
            ],
          ),
          if (_campaigns.isNotEmpty) _campaignSelector(),
          SizedBox(
            width: 280,
            child: TextField(
              controller: _searchController,
              decoration: InputDecoration(
                labelText: 'Rechercher un produit',
                prefixIcon: const Icon(Icons.search),
                suffixIcon: _query.isEmpty ? null : IconButton(onPressed: _searchController.clear, icon: const Icon(Icons.clear)),
                border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
              ),
            ),
          ),
          OutlinedButton.icon(
            onPressed: _loadingRows ? null : _loadRows,
            icon: const Icon(Icons.refresh),
            label: const Text('Actualiser'),
          ),
          OutlinedButton.icon(
            onPressed: _loadingRows ? null : _exportBestComplianceXlsx,
            icon: const Icon(Icons.file_download_outlined),
            label: const Text('Excel conformite'),
          ),
          OutlinedButton.icon(
            onPressed: _loadingRows ? null : _exportLowestPriceXlsx,
            icon: const Icon(Icons.price_change_outlined),
            label: const Text('Excel moins cher'),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final rows = _displayRows();
    return Column(
      children: [
        _buildHeader(),
        if (_loadingCampaigns || _loadingRows) const LinearProgressIndicator(minHeight: 2),
        Expanded(
          child: (_loadingCampaigns || (_loadingRows && rows.isEmpty))
              ? const Center(child: CircularProgressIndicator())
              : _error != null
                  ? EmptyState(
                      icon: Icons.error_outline,
                      title: 'Erreur de chargement',
                      message: _error!,
                      action: FilledButton(onPressed: _loadCampaigns, child: const Text('Reessayer')),
                    )
                  : rows.isEmpty
                      ? const EmptyState(
                          icon: Icons.smart_toy_outlined,
                          title: 'Aucune analyse',
                          message: 'Aucune offre exploitable pour cette campagne avec les donnees actuelles.',
                        )
                      : Padding(
                          padding: const EdgeInsets.all(16),
                          child: ListView.builder(
                            itemCount: rows.length,
                            itemBuilder: (context, index) => _buildProductCard(rows[index], index + 1),
                          ),
                        ),
        ),
      ],
    );
  }
}

