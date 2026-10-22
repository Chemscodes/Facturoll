import 'dart:async';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';

import '../api/campaigns_api.dart';
import '../api/client.dart';
import '../utils/file_opening.dart';
import '../widgets/empty_state.dart';

class StrictCampaignsScreen extends StatefulWidget {
  const StrictCampaignsScreen({super.key});

  @override
  State<StrictCampaignsScreen> createState() => _StrictCampaignsScreenState();
}

class _StrictCampaignsScreenState extends State<StrictCampaignsScreen> {
  static const int _pageSize = 50;

  final TextEditingController _searchController = TextEditingController();

  bool _loadingCampaigns = true;
  bool _loadingRows = false;
  int _rowsRequestId = 0;
  String? _error;
  List<dynamic> _campaigns = [];
  List<dynamic> _rows = [];
  bool _cacheReady = true;
  Timer? _countdownTicker;

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

  int? _selectedCampaignId;
  int? _rowsCampaignId;
  String _searchQuery = '';
  bool _onlyStrictData = false;
  final Map<int, int> _selectedOfferByProductId = <int, int>{};
  final Set<int> _marketEstimateLoadingProductIds = <int>{};

  void _showError(Object err) {
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
  }

  Future<bool> _openWithShell(String path) async {
    return openWithShell(path);
  }

  Future<void> _openLocalFile(String? path) async {
    final value = (path ?? '').trim();
    if (value.isEmpty) return;
    final opened = await _openWithShell(value);
    if (!opened && mounted) {
      _showError(Exception('Impossible d\'ouvrir le fichier'));
    }
  }

  Future<bool> _tryOpenLocalFile(String path) async {
    return _openWithShell(path);
  }

  bool _isLocalWindowsPath(String value) {
    return isLocalWindowsPath(value);
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
      prefix: 'strict',
    );
    await _openLocalFile(path);
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

  Widget _campaignSelector() {
    final selectedCampaign = _selectedCampaignMap();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        ConstrainedBox(
          constraints: const BoxConstraints(minWidth: 240, maxWidth: 460),
          child: DropdownButtonFormField<int>(
            value: _selectedCampaignId,
            decoration: const InputDecoration(
              labelText: 'Campaign',
              border: OutlineInputBorder(),
              isDense: true,
            ),
            items: _campaigns.map<DropdownMenuItem<int>>((raw) {
              if (raw is! Map) {
                return const DropdownMenuItem<int>(
                  value: -1,
                  child: Text('-'),
                );
              }
              final idRaw = raw['id'];
              final id = idRaw is num ? idRaw.toInt() : -1;
              final title = _campaignDisplayLabel(raw);
              return DropdownMenuItem<int>(
                value: id,
                child: Text(
                  title,
                  overflow: TextOverflow.ellipsis,
                  maxLines: 1,
                ),
              );
            }).toList(),
            onChanged: (value) async {
              if (value == null || value == _selectedCampaignId) return;
              setState(() {
                _selectedCampaignId = value;
                _selectedOfferByProductId.clear();
              });
              await _loadRows();
            },
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
        background: Theme.of(context)
            .colorScheme
            .surfaceContainerHighest
            .withValues(alpha: 0.45),
        border: Theme.of(context).colorScheme.outline.withValues(alpha: 0.20),
        text: Theme.of(context).colorScheme.onSurface,
      );
    }
    final now = DateTime.now();
    final target =
        DateTime(value.year, value.month, value.day, 23, 59, 59).toLocal();
    final diff = target.difference(now);
    if (diff.isNegative) {
      return (
        background: const Color(0xFFFDECEC),
        border: const Color(0xFFF5C2C7),
        text: const Color(0xFFB42318),
      );
    }
    if (diff.inDays <= 1) {
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

  Widget _campaignMetaBanner() {
    final campaign = _selectedCampaignMap();
    if (campaign == null) return const SizedBox.shrink();
    final countdownColors = _campaignCountdownColors(campaign['deposit_date']);
    final processing = [
      if (campaign['processing_in_progress'] == true) 'En cours',
      if (campaign['processing_validated'] == true) 'Valide',
    ].join(' / ');
    return Container(
      margin: const EdgeInsets.only(top: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _infoPill('Etablissement',
                  (campaign['establishment_name'] ?? '-').toString()),
              _infoPill('Lot', (campaign['lot_reference'] ?? '-').toString()),
              _infoPill(
                  'Type', (campaign['establishment_type'] ?? '-').toString()),
              _infoPill('Depot', _formatCampaignDate(campaign['deposit_date'])),
              _infoPill(
                'Compte a rebours',
                _campaignCountdown(campaign['deposit_date']),
                backgroundColor: countdownColors.background,
                borderColor: countdownColors.border,
                textColor: countdownColors.text,
              ),
              _infoPill('Traitement', processing.isEmpty ? '-' : processing),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            'Adresse: ${(campaign['establishment_address'] ?? '-').toString()}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
          const SizedBox(height: 4),
          Text(
            'Telephone: ${(campaign['establishment_phone'] ?? '-').toString()} | Pharmacie: ${(campaign['pharmacy_service'] ?? '-').toString()} | Bureau de marche: ${(campaign['market_office_service'] ?? '-').toString()}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ),
    );
  }

  @override
  void initState() {
    super.initState();
    _startCountdownTicker();
    _searchController.addListener(() {
      if (!mounted) return;
      setState(
          () => _searchQuery = _searchController.text.trim().toLowerCase());
    });
    _loadCampaigns();
  }

  @override
  void dispose() {
    _countdownTicker?.cancel();
    _searchController.dispose();
    super.dispose();
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
        final mode =
            (raw['processing_mode'] ?? '').toString().trim().toLowerCase();
        return mode == 'strict';
      }).toList()
        ..sort((a, b) {
          final aId =
              a is Map && a['id'] is num ? (a['id'] as num).toInt() : -1;
          final bId =
              b is Map && b['id'] is num ? (b['id'] as num).toInt() : -1;
          return bId.compareTo(aId);
        });
      int? selected = _selectedCampaignId;
      if (campaigns.isNotEmpty) {
        final exists = campaigns.any((raw) {
          if (raw is! Map) return false;
          final idRaw = raw['id'];
          return idRaw is num && idRaw.toInt() == selected;
        });
        if (!exists) {
          final first = campaigns.first;
          selected = first is Map && first['id'] is num
              ? (first['id'] as num).toInt()
              : null;
        }
      } else {
        selected = null;
      }
      setState(() {
        _campaigns = campaigns;
        _selectedCampaignId = selected;
        if (selected == null) {
          _rows = [];
          _rowsCampaignId = null;
          _selectedOfferByProductId.clear();
        }
        _loadingCampaigns = false;
      });
      if (selected != null && (_rowsCampaignId != selected || _rows.isEmpty)) {
        unawaited(_loadRows());
      }
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
      setState(() {
        _rows = [];
        _cacheReady = true;
        _rowsCampaignId = null;
        _selectedOfferByProductId.clear();
      });
      return;
    }
    final requestId = ++_rowsRequestId;
    final shouldClearRows = _rowsCampaignId != campaignId;
    setState(() {
      _loadingRows = true;
      _error = null;
      if (shouldClearRows) {
        _rows = [];
      }
    });
    try {
      final firstPayload = await CampaignsApi.offersByProduct(
        campaignId,
        conformOnly: false,
        includeMarketEstimates: false,
        cacheOnly: true,
        page: 1,
        pageSize: _pageSize,
      );
      if (!mounted || requestId != _rowsRequestId) return;

      final allRows =
          List<dynamic>.from((firstPayload['items'] as List?) ?? const []);
      final cacheReady = firstPayload['cache_ready'] == true;
      var page = 2;
      var hasMore = cacheReady && firstPayload['has_more'] == true;

      setState(() {
        _cacheReady = cacheReady;
        _rows = List<dynamic>.from(allRows);
      });
      _rowsCampaignId = campaignId;
      _pruneSelectedOffers();

      if (!cacheReady) {
        final warmPayload = await CampaignsApi.offersByProduct(
          campaignId,
          conformOnly: false,
          includeMarketEstimates: false,
          cacheOnly: false,
          page: 1,
          pageSize: _pageSize,
        );
        if (!mounted || requestId != _rowsRequestId) return;

        allRows
          ..clear()
          ..addAll(
            List<dynamic>.from((warmPayload['items'] as List?) ?? const []),
          );
        hasMore = warmPayload['has_more'] == true;

        setState(() {
          _cacheReady = true;
          _rows = List<dynamic>.from(allRows);
        });
        _rowsCampaignId = campaignId;
        _pruneSelectedOffers();
        page = 2;
      }

      while (hasMore && page <= 200) {
        final payload = await CampaignsApi.offersByProduct(
          campaignId,
          conformOnly: false,
          includeMarketEstimates: false,
          cacheOnly: true,
          page: page,
          pageSize: _pageSize,
        );
        if (!mounted || requestId != _rowsRequestId) return;

        final items =
            List<dynamic>.from((payload['items'] as List?) ?? const []);
        allRows.addAll(items);
        hasMore = payload['has_more'] == true;
        page += 1;

        setState(() => _rows = List<dynamic>.from(allRows));
        _rowsCampaignId = campaignId;
        _pruneSelectedOffers();
      }
    } catch (err) {
      if (!mounted || requestId != _rowsRequestId) return;
      setState(() => _error = ApiClient.formatError(err));
    } finally {
      if (mounted && requestId == _rowsRequestId) {
        setState(() => _loadingRows = false);
      }
    }
  }

  Future<void> _exportStrictTopOffersXlsx() async {
    final id = _selectedCampaignId;
    if (id == null) return;
    try {
      final bytes = await CampaignsApi.exportStrictTopOffersXlsx(id);
      final campaign = _campaigns.firstWhere(
        (c) => c['id'] == id,
        orElse: () => null,
      );
      final title =
          campaign == null ? 'campaign_$id' : (campaign['title'] as String);
      final fileName = '${title.replaceAll(' ', '_')}_strict_prix_bas.xlsx';
      final path = await FilePicker.platform.saveFile(
        dialogTitle: 'Enregistrer Excel Strict - prix bas',
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

  Future<void> _exportStrictTopOffersByColorXlsx() async {
    final id = _selectedCampaignId;
    if (id == null) return;
    try {
      final bytes = await CampaignsApi.exportStrictTopOffersByColorXlsx(id);
      final campaign = _campaigns.firstWhere(
        (c) => c['id'] == id,
        orElse: () => null,
      );
      final title =
          campaign == null ? 'campaign_$id' : (campaign['title'] as String);
      final fileName =
          '${title.replaceAll(' ', '_')}_strict_conformite.xlsx';
      final path = await FilePicker.platform.saveFile(
        dialogTitle: 'Enregistrer Excel Strict - conformite',
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

  Future<void> _exportStrictSelectedOffersXlsx() async {
    final id = _selectedCampaignId;
    if (id == null) return;
    try {
      final bytes = await CampaignsApi.exportStrictSelectedOffersXlsx(
        id,
        _selectedOfferByProductId,
      );
      final campaign = _campaigns.firstWhere(
        (c) => c['id'] == id,
        orElse: () => null,
      );
      final title =
          campaign == null ? 'campaign_$id' : (campaign['title'] as String);
      final fileName =
          '${title.replaceAll(' ', '_')}_strict_selected_offers.xlsx';
      final path = await FilePicker.platform.saveFile(
        dialogTitle: 'Save Strict Excel with selected offers',
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

  Future<void> _openInvoiceFile(Map<String, dynamic> offer) async {
    final invoicePath = (offer['invoice_file_path'] ?? '').toString().trim();
    final invoiceFile = (offer['invoice_file_name'] ?? '').toString().trim();
    final invoiceIdRaw = offer['invoice_id'];
    final invoiceId = invoiceIdRaw is num ? invoiceIdRaw.toInt() : null;

    if (invoicePath.isNotEmpty &&
        (_isLocalWindowsPath(invoicePath) ||
            invoicePath.startsWith('http://') ||
            invoicePath.startsWith('https://'))) {
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
        return;
      }
    }

    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Facture introuvable')),
    );
  }

  Future<void> _openRequestedSourceAttachment(
    Map<String, dynamic> product,
  ) async {
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

  Future<void> _generateMarketEstimateOffer(
    Map<String, dynamic> product,
  ) async {
    final campaignId = _selectedCampaignId;
    final productIdRaw = product['product_id'];
    final productId = productIdRaw is num ? productIdRaw.toInt() : null;
    if (campaignId == null || productId == null) return;
    if (_marketEstimateLoadingProductIds.contains(productId)) return;
    if (_hasMarketEstimateOffer(product)) return;

    setState(() => _marketEstimateLoadingProductIds.add(productId));
    try {
      final offer = await CampaignsApi.generateMarketEstimateOffer(
        campaignId,
        productId,
      );
      if (!mounted) return;
      setState(() {
        for (final raw in _rows) {
          if (raw is! Map) continue;
          final rawProductId = raw['product_id'];
          if (rawProductId is! num || rawProductId.toInt() != productId) {
            continue;
          }
          final offers =
              List<dynamic>.from((raw['offers'] as List?) ?? const []);
          offers.removeWhere(
              (item) => item is Map && item['market_estimate'] == true);
          offers.add(Map<String, dynamic>.from(offer));
          raw['offers'] = offers;
          if (raw['best_offer'] == null) {
            raw['best_offer'] = Map<String, dynamic>.from(offer);
          }
          break;
        }
      });
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Estimation marche DZ generee avec GPT 5.1'),
        ),
      );
    } catch (err) {
      if (!mounted) return;
      _showError(err);
    } finally {
      if (mounted) {
        setState(() => _marketEstimateLoadingProductIds.remove(productId));
      }
    }
  }

  void _pruneSelectedOffers() {
    final validSelections = <int, int>{};
    for (final rowRaw in _rows) {
      final row = _rowMap(rowRaw);
      final productIdRaw = row?['product_id'];
      final productId = productIdRaw is num ? productIdRaw.toInt() : null;
      if (productId == null) continue;
      final selectedInvoiceItemId = _selectedOfferByProductId[productId];
      if (selectedInvoiceItemId == null) continue;
      final hasOffer = _eligibleOffersForRow(rowRaw).any((offer) {
        final offerId = _offerInvoiceItemId(offer);
        return offerId != null && offerId == selectedInvoiceItemId;
      });
      if (hasOffer) {
        validSelections[productId] = selectedInvoiceItemId;
      }
    }
    if (validSelections.length != _selectedOfferByProductId.length) {
      _selectedOfferByProductId
        ..clear()
        ..addAll(validSelections);
    }
  }

  List<Map<String, dynamic>> _offersForRow(dynamic rowRaw) {
    if (rowRaw is! Map) return const [];
    final offersRaw = rowRaw['offers'];
    if (offersRaw is! List) return const [];
    final out = <Map<String, dynamic>>[];
    for (final raw in offersRaw) {
      if (raw is Map) {
        out.add(Map<String, dynamic>.from(raw));
      }
    }
    return out;
  }

  int? _offerInvoiceItemId(Map<String, dynamic>? offer) {
    if (offer == null) return null;
    final raw = offer['invoice_item_id'];
    return raw is num ? raw.toInt() : null;
  }

  bool _isMarketEstimateOffer(Map<String, dynamic>? offer) {
    if (offer == null) return false;
    return offer['market_estimate'] == true;
  }

  bool _hasMarketEstimateOffer(dynamic rowRaw) {
    final offers = _offersForRow(rowRaw);
    return offers.any(_isMarketEstimateOffer);
  }

  bool _passesStrictThreshold(Map<String, dynamic> offer) {
    if (_isMarketEstimateOffer(offer)) return true;
    final score = _strictScoreForOffer(offer);
    final threshold = _toDouble(offer['strict_match_threshold']) ?? 0.65;
    return score >= threshold;
  }

  List<Map<String, dynamic>> _eligibleOffersForRow(dynamic rowRaw) {
    final offers = _offersForRow(rowRaw);
    return offers.where(_passesStrictThreshold).toList();
  }

  bool _hasStrictData(dynamic rowRaw) {
    final offers = _eligibleOffersForRow(rowRaw);
    for (final offer in offers) {
      if (offer['strict_final_decision'] != null) return true;
      if (offer['strict_match_score'] != null) return true;
      final alerts = offer['strict_alerts'];
      if (alerts is List && alerts.isNotEmpty) return true;
    }
    return false;
  }

  double _strictScoreForOffer(Map<String, dynamic> offer) {
    final scoreRaw = offer['strict_match_score'];
    if (scoreRaw is num) return scoreRaw.toDouble();
    if (scoreRaw is String) {
      return double.tryParse(scoreRaw.trim().replaceAll(',', '.')) ?? -1.0;
    }
    return -1.0;
  }

  int _strictOfferColorPriority(Map<String, dynamic> offer) {
    final alertColors = _extractAlertColors(offer);
    final primary = _primaryColorFromAlertColors(alertColors.values);
    switch (primary) {
      case 'green':
        return 0;
      case 'yellow':
        return 1;
      case 'gray':
        return 2;
      case 'purple':
        return 3;
      case 'orange':
        return 4;
      case 'red':
        return 5;
      default:
        return 6;
    }
  }

  Map<String, dynamic>? _bestStrictOffer(dynamic rowRaw) {
    final offers = _eligibleOffersForRow(rowRaw);
    if (offers.isEmpty) return null;
    Map<String, dynamic>? best;
    var bestColorPriority = 999;
    var bestScore = -1.0;
    for (final offer in offers) {
      final colorPriority = _strictOfferColorPriority(offer);
      final hasStrictDecision =
          (offer['strict_final_decision'] ?? '').toString().trim().isNotEmpty;
      final score = _strictScoreForOffer(offer);
      if (best == null) {
        best = offer;
        bestColorPriority = colorPriority;
        bestScore = score;
        continue;
      }
      if (colorPriority < bestColorPriority) {
        best = offer;
        bestColorPriority = colorPriority;
        bestScore = score;
        continue;
      }
      if (colorPriority > bestColorPriority) {
        continue;
      }
      final bestHasStrictDecision =
          (best['strict_final_decision'] ?? '').toString().trim().isNotEmpty;
      if (hasStrictDecision && !bestHasStrictDecision) {
        best = offer;
        bestColorPriority = colorPriority;
        bestScore = score;
        continue;
      }
      if (hasStrictDecision == bestHasStrictDecision && score > bestScore) {
        best = offer;
        bestColorPriority = colorPriority;
        bestScore = score;
      }
    }
    return best;
  }

  Map<String, String> _extractAlertColors(Map<String, dynamic>? offer) {
    if (offer == null) return const {};
    final strictColorsRaw = offer['strict_alert_colors'];
    if (strictColorsRaw is Map) {
      final out = <String, String>{};
      strictColorsRaw.forEach((key, value) {
        final k = key.toString().trim();
        final v = value.toString().trim().toLowerCase();
        if (k.isNotEmpty && v.isNotEmpty) {
          out[k] = v;
        }
      });
      if (out.isNotEmpty) return out;
    }
    final fallbackRaw = offer['match_badge_breakdown'];
    if (fallbackRaw is Map) {
      final out = <String, String>{};
      fallbackRaw.forEach((key, value) {
        if (value is! Map) return;
        final color = (value['color'] ?? '').toString().trim().toLowerCase();
        final badge = key.toString().trim();
        if (badge.isNotEmpty && color.isNotEmpty) {
          out[badge] = color;
        }
      });
      return out;
    }
    return const {};
  }

  String _primaryColorFromAlertColors(Iterable<String> rawColors) {
    final values = <String>{};
    for (final raw in rawColors) {
      final token = raw.trim().toLowerCase();
      if (token.isNotEmpty) values.add(token);
    }
    final candidates =
        values.where((c) => !{'blue', 'amber', 'purple'}.contains(c)).toSet();
    if (candidates.contains('red')) return 'red';
    if (candidates.contains('orange')) return 'orange';
    if (candidates.contains('gray')) return 'gray';
    if (candidates.contains('yellow')) return 'yellow';
    if (candidates.contains('green')) return 'green';
    if (values.contains('purple')) return 'purple';
    if (values.contains('green')) return 'green';
    return 'gray';
  }

  Map<String, dynamic>? _rowMap(dynamic rowRaw) {
    if (rowRaw is Map) return Map<String, dynamic>.from(rowRaw);
    return null;
  }

  String _requestedStrictLabel(dynamic rowRaw, Map<String, dynamic>? best) {
    final row = _rowMap(rowRaw);
    final strict =
        (best?['requested_strict_name_used'] ?? '').toString().trim();
    if (strict.isNotEmpty) return strict;
    final fallback = (row?['product_name'] ?? '').toString().trim();
    return fallback.isEmpty ? '-' : fallback;
  }

  double? _toDouble(dynamic value) {
    if (value is num) return value.toDouble();
    if (value is String) {
      return double.tryParse(value.trim().replaceAll(',', '.'));
    }
    return null;
  }

  String _price(dynamic value) {
    final number = _toDouble(value);
    if (number == null) return '-';
    if (number == number.roundToDouble()) return number.toInt().toString();
    return number.toStringAsFixed(2).replaceAll('.', ',');
  }

  String _qty(dynamic value) {
    final number = _toDouble(value);
    if (number == null) return '-';
    if (number == number.roundToDouble()) return number.toInt().toString();
    return number.toStringAsFixed(2).replaceAll('.', ',');
  }

  String _offeredQtyStrictLabel(Map<String, dynamic>? offer) {
    if (offer == null) return '-';
    final strictQty = offer['offered_qty_strict'];
    if (_toDouble(strictQty) != null) return _qty(strictQty);
    return _qty(offer['offered_qty']);
  }

  String _offerSupplierLabel(Map<String, dynamic>? offer) {
    if (offer == null) return '-';
    final name = (offer['supplier_name'] ?? '').toString().trim();
    return name.isEmpty ? '-' : name;
  }

  String _offerSupplierPhone(Map<String, dynamic>? offer) {
    if (offer == null) return '-';
    final phone = (offer['supplier_phone'] ?? '').toString().trim();
    return phone.isEmpty ? '-' : phone;
  }

  String _offerSupplierEmail(Map<String, dynamic>? offer) {
    if (offer == null) return '-';
    final email = (offer['supplier_email'] ??
            offer['invoice_sender_email'] ??
            offer['sender_email'] ??
            '')
        .toString()
        .trim();
    return email.isEmpty ? '-' : email;
  }

  String _badgesLabel(Map<String, dynamic>? offer) {
    if (offer == null) return '-';
    final strictAlerts = offer['strict_alerts'];
    if (strictAlerts is List && strictAlerts.isNotEmpty) {
      return strictAlerts.map((e) => e.toString()).join(', ');
    }
    final badges = offer['match_badges'];
    if (badges is List && badges.isNotEmpty) {
      return badges.map((e) => e.toString()).join(', ');
    }
    return '-';
  }

  Color _colorForToken(String token) {
    switch (token.toLowerCase()) {
      case 'red':
        return const Color(0xFFB42318);
      case 'orange':
        return const Color(0xFFB54708);
      case 'yellow':
        return const Color(0xFFB17B00);
      case 'green':
        return const Color(0xFF027A48);
      case 'blue':
        return const Color(0xFF175CD3);
      case 'amber':
        return const Color(0xFFB93815);
      case 'purple':
        return const Color(0xFF6941C6);
      case 'gray':
      default:
        return const Color(0xFF667085);
    }
  }

  Widget _strictPenaltyChip(Map<String, dynamic>? offer) {
    if (offer == null || offer['strict_penalty_applied'] != true) {
      return const SizedBox.shrink();
    }
    final penaltyValue = _toDouble(offer['strict_penalty_value']);
    final labelsRaw = offer['strict_penalty_labels'];
    final labels = labelsRaw is List
        ? labelsRaw
            .map((e) => e?.toString().trim() ?? '')
            .where((e) => e.isNotEmpty)
            .toList()
        : const <String>[];
    final suffix = labels.isEmpty ? '' : ' ${labels.join(", ")}';
    final text = penaltyValue == null
        ? 'Produit considere faible$suffix'
        : 'Produit considere faible -${penaltyValue.toStringAsFixed(2)}$suffix';
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: const Color(0xFFFFF4E5),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(
          color: const Color(0xFFF59E0B).withValues(alpha: 0.45),
        ),
      ),
      child: Text(
        text,
        style: const TextStyle(
          color: Color(0xFFB45309),
          fontWeight: FontWeight.w700,
        ),
      ),
    );
  }

  String _labelForPrimaryColor(String token) {
    switch (token.toLowerCase()) {
      case 'red':
        return 'Non compensable';
      case 'orange':
        return 'Unite incompatible';
      case 'yellow':
        return 'Unite inferee';
      case 'green':
        return 'Compatible';
      case 'purple':
        return 'Estimation marche DZ';
      case 'gray':
      default:
        return 'Info incomplete';
    }
  }

  Widget _metricTile(String label, String value, Color accent) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: accent.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(12),
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
            style: const TextStyle(
              fontSize: 15,
              fontWeight: FontWeight.w800,
              color: Color(0xFF101828),
            ),
          ),
        ],
      ),
    );
  }

  String _fmt(dynamic value) {
    if (value == null) return '-';
    if (value is num) {
      if (value == value.roundToDouble()) return value.toInt().toString();
      return value.toStringAsFixed(2).replaceAll('.', ',');
    }
    final text = value.toString().trim();
    return text.isEmpty ? '-' : text;
  }

  List<dynamic> get _filteredRows {
    final query = _searchQuery;
    return _rows.where((row) {
      final eligibleOffers = _eligibleOffersForRow(row);
      if (_onlyStrictData && !_hasStrictData(row)) return false;
      if (query.isEmpty) return true;
      final best = _bestStrictOffer(row);
      final strictName = _requestedStrictLabel(row, best).toLowerCase();
      if (strictName.contains(query)) return true;
      final rowMap = _rowMap(row);
      final catalogName =
          (rowMap?['product_name'] ?? '').toString().toLowerCase();
      if (catalogName.contains(query)) return true;
      for (final offer in eligibleOffers) {
        final offeredName =
            (offer['extracted_name'] ?? '').toString().toLowerCase();
        if (offeredName.contains(query)) return true;
      }
      return false;
    }).toList();
  }

  Widget _buildHeader() {
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 14, 16, 10),
      decoration: BoxDecoration(
        color: Theme.of(context).colorScheme.surface,
        border: Border(
          bottom: BorderSide(
            color: Theme.of(context).colorScheme.outline.withValues(alpha: 0.2),
          ),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.rule_folder_outlined),
              const SizedBox(width: 10),
              Text(
                'Strict Campaigns',
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const Spacer(),
              IconButton(
                tooltip: 'Reload',
                onPressed: _loadingRows ? null : _loadRows,
                icon: const Icon(Icons.refresh),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 12,
            runSpacing: 10,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              _campaignSelector(),
              SizedBox(
                width: 320,
                child: TextField(
                  controller: _searchController,
                  decoration: const InputDecoration(
                    labelText: 'Search requested product',
                    prefixIcon: Icon(Icons.search),
                    border: OutlineInputBorder(),
                    isDense: true,
                  ),
                ),
              ),
              if (_selectedCampaignId != null)
                FilledButton.tonalIcon(
                  onPressed: _exportStrictTopOffersXlsx,
                  icon: const Icon(Icons.table_chart_outlined),
                  label: const Text('Excel prix bas'),
                ),
              if (_selectedCampaignId != null)
                FilledButton.tonalIcon(
                  onPressed: _exportStrictTopOffersByColorXlsx,
                  icon: const Icon(Icons.grid_view_outlined),
                  label: const Text('Excel conformite'),
                ),
              if (_selectedCampaignId != null)
                FilledButton.icon(
                  onPressed: _selectedOfferByProductId.isEmpty
                      ? null
                      : _exportStrictSelectedOffersXlsx,
                  icon: const Icon(Icons.checklist_rtl_outlined),
                  label: Text(
                    _selectedOfferByProductId.isEmpty
                        ? 'Exporter Excel Selection'
                        : 'Exporter Excel Selection (${_selectedOfferByProductId.length})',
                  ),
                ),
              FilterChip(
                label: const Text('Strict data only'),
                selected: _onlyStrictData,
                onSelected: (value) => setState(() => _onlyStrictData = value),
              ),
            ],
          ),
          _campaignMetaBanner(),
        ],
      ),
    );
  }

  Widget _buildProductCard(dynamic rowRaw) {
    final row =
        rowRaw is Map ? Map<String, dynamic>.from(rowRaw) : <String, dynamic>{};
    final productIdRaw = row['product_id'];
    final productId = productIdRaw is num ? productIdRaw.toInt() : 0;
    final selectedInvoiceItemId = _selectedOfferByProductId[productId];
    final requestedQty = row['requested_qty'];
    final offers = _eligibleOffersForRow(row);
    final best = _bestStrictOffer(row);
    final requestedStrictName = _requestedStrictLabel(row, best);
    final requestedCatalogName =
        (row['product_name'] ?? '-').toString().trim().isEmpty
            ? '-'
            : (row['product_name'] ?? '-').toString().trim();

    final alertColors = _extractAlertColors(best);
    final primaryColorToken = _primaryColorFromAlertColors(alertColors.values);
    final primaryColor = _colorForToken(primaryColorToken);
    final primaryLabel = _labelForPrimaryColor(primaryColorToken);
    final bestPenaltyApplied = best?['strict_penalty_applied'] == true;

    final strictDecision = (best?['strict_final_decision'] ??
            best?['strict_match_decision'] ??
            '-')
        .toString();
    final strictReason =
        (best?['strict_decision_reason'] ?? best?['strict_match_reason'] ?? '-')
            .toString();
    final strictScore = _fmt(best?['strict_match_score']);
    final convMode = _fmt(
        best?['strict_conversion_mode'] ?? best?['match_compensation_mode']);
    final convFactor = _fmt(best?['strict_conversion_factor'] ??
        best?['match_compensation_factor']);
    final offeredQty = _offeredQtyStrictLabel(best);
    final supplierName = _offerSupplierLabel(best);
    final supplierPhone = _offerSupplierPhone(best);
    final supplierEmail = _offerSupplierEmail(best);
    final offeredName =
        (best?['extracted_name'] ?? '-').toString().trim().isEmpty
            ? '-'
            : (best?['extracted_name'] ?? '-').toString().trim();
    final unitPrice = _price(best?['unit_price']);
    final equivalentUnitPrice = _price(best?['equivalent_unit_price']);
    final currency = (best?['currency'] ?? '').toString().trim();
    final requestedQtyValue = _toDouble(requestedQty);
    final bestEquivalentUnitPriceValue =
        _toDouble(best?['equivalent_unit_price']);
    final requestedSourcePath =
        (row['requested_source_attachment_path'] ?? '').toString().trim();
    final requestedSourceFile =
        (row['requested_source_attachment_file'] ?? '').toString().trim();
    final requestedSourceIdRaw = row['requested_source_attachment_id'];
    final requestedSourceId =
        requestedSourceIdRaw is num ? requestedSourceIdRaw.toInt() : null;
    final hasRequestedSource =
        requestedSourcePath.isNotEmpty || requestedSourceId != null;
    final totalEquivalentRequested = (requestedQtyValue != null &&
            bestEquivalentUnitPriceValue != null &&
            requestedQtyValue > 0)
        ? requestedQtyValue * bestEquivalentUnitPriceValue
        : null;
    final badgesLabel = _badgesLabel(best);
    final requestedStrictLine =
        (best?['requested_strict_line_used'] ?? '').toString().trim();
    final hasMarketEstimate = _hasMarketEstimateOffer(row);
    final marketEstimateLoading =
        _marketEstimateLoadingProductIds.contains(productId);

    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(18),
        side: BorderSide(
          color: primaryColor.withValues(alpha: 0.30),
        ),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
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
                      Text(
                        '$productId - $requestedStrictName',
                        style: Theme.of(context)
                            .textTheme
                            .titleMedium
                            ?.copyWith(fontWeight: FontWeight.w700),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        'Catalogue: $requestedCatalogName',
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                      const SizedBox(height: 8),
                      Text(
                        'Choix offert',
                        style:
                            Theme.of(context).textTheme.labelMedium?.copyWith(
                                  fontWeight: FontWeight.w700,
                                  color: const Color(0xFF475467),
                                ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        offeredName,
                        style: Theme.of(context).textTheme.titleLarge?.copyWith(
                              fontWeight: FontWeight.w800,
                              color: const Color(0xFF101828),
                            ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        'Qte demandee: ${_qty(requestedQty)} | Qte offerte: $offeredQty | Offres: ${offers.length}',
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                      const SizedBox(height: 10),
                      Container(
                        padding: const EdgeInsets.all(10),
                        decoration: BoxDecoration(
                          color: primaryColor.withValues(alpha: 0.08),
                          borderRadius: BorderRadius.circular(12),
                          border: Border.all(
                            color: primaryColor.withValues(alpha: 0.22),
                          ),
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              supplierName,
                              style: Theme.of(context)
                                  .textTheme
                                  .titleMedium
                                  ?.copyWith(
                                    fontWeight: FontWeight.w900,
                                    color: const Color(0xFF101828),
                                  ),
                            ),
                            const SizedBox(height: 4),
                            Text(
                              'Telephone: $supplierPhone',
                              style: Theme.of(context).textTheme.bodyMedium,
                            ),
                            const SizedBox(height: 2),
                            Text(
                              'Email: $supplierEmail',
                              style: Theme.of(context).textTheme.bodyMedium,
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
                Column(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 10, vertical: 6),
                      decoration: BoxDecoration(
                        color: primaryColor.withValues(alpha: 0.12),
                        borderRadius: BorderRadius.circular(999),
                        border: Border.all(
                            color: primaryColor.withValues(alpha: 0.45)),
                      ),
                      child: Text(
                        primaryLabel,
                        style: TextStyle(
                          color: primaryColor,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    if (bestPenaltyApplied) ...[
                      const SizedBox(height: 8),
                      _strictPenaltyChip(best),
                    ],
                  ],
                ),
              ],
            ),
            const SizedBox(height: 10),
            Wrap(
              spacing: 10,
              runSpacing: 10,
              children: [
                _infoPill('Strict decision', strictDecision),
                _infoPill('Reason', strictReason),
                _infoPill('Strict score', strictScore),
                _infoPill('Conv mode', convMode),
                _infoPill('Factor', convFactor),
                _infoPill('PU facture', unitPrice),
                _infoPill('PU eq.', equivalentUnitPrice),
              ],
            ),
            const SizedBox(height: 10),
            Wrap(
              spacing: 10,
              runSpacing: 10,
              children: [
                SizedBox(
                  width: 180,
                  child: _metricTile(
                      'Qte demandee', _qty(requestedQty), primaryColor),
                ),
                SizedBox(
                  width: 180,
                  child: _metricTile('Qte offerte', offeredQty, primaryColor),
                ),
                SizedBox(
                  width: 180,
                  child: _metricTile(
                      'Offres', offers.length.toString(), primaryColor),
                ),
                SizedBox(
                  width: 220,
                  child: _metricTile(
                    'Total eq. demande',
                    totalEquivalentRequested != null
                        ? '${_price(totalEquivalentRequested)} ${currency.isEmpty ? '' : currency}'
                            .trim()
                        : '-',
                    primaryColor,
                  ),
                ),
                if (selectedInvoiceItemId != null)
                  SizedBox(
                    width: 220,
                    child: _metricTile(
                      'Selection manuelle',
                      'Item $selectedInvoiceItemId',
                      primaryColor,
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 10),
            FilledButton.tonalIcon(
              onPressed: (hasMarketEstimate || marketEstimateLoading)
                  ? null
                  : () => _generateMarketEstimateOffer(row),
              icon: marketEstimateLoading
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.travel_explore_outlined, size: 16),
              label: Text(
                hasMarketEstimate
                    ? 'Estimation marche DZ deja generee'
                    : marketEstimateLoading
                        ? 'Generation GPT 5.1...'
                        : 'Estimation marche DZ (GPT 5.1)',
              ),
            ),
            if (hasRequestedSource) ...[
              const SizedBox(height: 10),
              OutlinedButton.icon(
                onPressed: () => _openRequestedSourceAttachment(row),
                icon: const Icon(Icons.attach_file_outlined, size: 16),
                label: Text(
                  requestedSourceFile.isEmpty
                      ? 'Voir PJ source'
                      : 'Ouvrir PJ source',
                ),
              ),
            ],
            if (requestedStrictLine.isNotEmpty) ...[
              const SizedBox(height: 10),
              SelectableText(
                requestedStrictLine,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
            if (alertColors.isNotEmpty) ...[
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: alertColors.entries.map((entry) {
                  final color = _colorForToken(entry.value);
                  return Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
                    decoration: BoxDecoration(
                      color: color.withValues(alpha: 0.12),
                      borderRadius: BorderRadius.circular(999),
                      border: Border.all(color: color.withValues(alpha: 0.42)),
                    ),
                    child: Text(
                      '${entry.key} (${entry.value})',
                      style: TextStyle(
                        fontSize: 12,
                        color: color,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  );
                }).toList(),
              ),
            ],
            const SizedBox(height: 10),
            Text(
              'Badges: $badgesLabel',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            if (offers.isNotEmpty) ...[
              const SizedBox(height: 8),
              ExpansionTile(
                tilePadding: EdgeInsets.zero,
                childrenPadding: EdgeInsets.zero,
                title: const Text('Show offers'),
                subtitle: Text('${offers.length} offer(s)'),
                children: offers.map((offer) {
                  final itemId = _fmt(offer['invoice_item_id']);
                  final invoiceItemId = _offerInvoiceItemId(offer);
                  final isSelected = invoiceItemId != null &&
                      selectedInvoiceItemId != null &&
                      invoiceItemId == selectedInvoiceItemId;
                  final offeredName =
                      (offer['extracted_name'] ?? '-').toString();
                  final score = _fmt(offer['strict_match_score']);
                  final decision = (offer['strict_final_decision'] ??
                          offer['strict_match_decision'] ??
                          '-')
                      .toString();
                  final offerQty = _offeredQtyStrictLabel(offer);
                  final offerSupplierName = _offerSupplierLabel(offer);
                  final offerSupplierPhone = _offerSupplierPhone(offer);
                  final offerSupplierEmail = _offerSupplierEmail(offer);
                  final offerPrice = _price(offer['unit_price']);
                  final offerEqPrice = _price(offer['equivalent_unit_price']);
                  final offerEqPriceValue =
                      _toDouble(offer['equivalent_unit_price']);
                  final invoicePath =
                      (offer['invoice_file_path'] ?? '').toString().trim();
                  final invoiceFile =
                      (offer['invoice_file_name'] ?? '').toString().trim();
                  final invoiceIdRaw = offer['invoice_id'];
                  final invoiceId =
                      invoiceIdRaw is num ? invoiceIdRaw.toInt() : null;
                  final hasInvoice =
                      invoicePath.isNotEmpty || invoiceId != null;
                  final offerTotalEquivalentRequested =
                      (requestedQtyValue != null &&
                              offerEqPriceValue != null &&
                              requestedQtyValue > 0)
                          ? requestedQtyValue * offerEqPriceValue
                          : null;
                  final offerColors = _extractAlertColors(offer);
                  final offerPrimaryToken =
                      _primaryColorFromAlertColors(offerColors.values);
                  final offerPrimaryColor = _colorForToken(offerPrimaryToken);
                  return Container(
                    margin: const EdgeInsets.only(bottom: 10),
                    padding: const EdgeInsets.all(14),
                    decoration: BoxDecoration(
                      gradient: LinearGradient(
                        begin: Alignment.topLeft,
                        end: Alignment.bottomRight,
                        colors: [
                          offerPrimaryColor.withValues(alpha: 0.14),
                          offerPrimaryColor.withValues(alpha: 0.05),
                        ],
                      ),
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(
                          color: isSelected
                              ? offerPrimaryColor.withValues(alpha: 0.70)
                              : offerPrimaryColor.withValues(alpha: 0.30),
                          width: isSelected ? 2 : 1),
                    ),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Expanded(
                              child: Text(
                                '$itemId - $offeredName',
                                style: const TextStyle(
                                  fontSize: 16,
                                  fontWeight: FontWeight.w800,
                                  color: Color(0xFF101828),
                                ),
                              ),
                            ),
                            Checkbox(
                              value: isSelected,
                              onChanged: invoiceItemId == null
                                  ? null
                                  : (checked) {
                                      setState(() {
                                        if (checked == true) {
                                          _selectedOfferByProductId[productId] =
                                              invoiceItemId;
                                        } else if (_selectedOfferByProductId[
                                                productId] ==
                                            invoiceItemId) {
                                          _selectedOfferByProductId
                                              .remove(productId);
                                        }
                                      });
                                    },
                            ),
                            Container(
                              padding: const EdgeInsets.symmetric(
                                  horizontal: 10, vertical: 6),
                              decoration: BoxDecoration(
                                color:
                                    offerPrimaryColor.withValues(alpha: 0.14),
                                borderRadius: BorderRadius.circular(999),
                                border: Border.all(
                                    color: offerPrimaryColor.withValues(
                                        alpha: 0.30)),
                              ),
                              child: Text(
                                _labelForPrimaryColor(offerPrimaryToken),
                                style: TextStyle(
                                  color: offerPrimaryColor,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                            ),
                            if (offer['strict_penalty_applied'] == true) ...[
                              const SizedBox(width: 8),
                              _strictPenaltyChip(offer),
                            ],
                          ],
                        ),
                        const SizedBox(height: 10),
                        Wrap(
                          spacing: 10,
                          runSpacing: 10,
                          children: [
                            SizedBox(
                              width: 150,
                              child: _metricTile(
                                  'Strict score', score, offerPrimaryColor),
                            ),
                            SizedBox(
                              width: 210,
                              child: _metricTile(
                                  'Decision', decision, offerPrimaryColor),
                            ),
                            SizedBox(
                              width: 150,
                              child: _metricTile('Qte demandee',
                                  _qty(requestedQty), offerPrimaryColor),
                            ),
                            SizedBox(
                              width: 150,
                              child: _metricTile(
                                  'Qte offerte', offerQty, offerPrimaryColor),
                            ),
                            SizedBox(
                              width: 150,
                              child: _metricTile(
                                  'PU facture', offerPrice, offerPrimaryColor),
                            ),
                            SizedBox(
                              width: 150,
                              child: _metricTile(
                                  'PU eq.', offerEqPrice, offerPrimaryColor),
                            ),
                            SizedBox(
                              width: 200,
                              child: _metricTile(
                                'Total eq. demande',
                                offerTotalEquivalentRequested != null
                                    ? '${_price(offerTotalEquivalentRequested)} ${currency.isEmpty ? '' : currency}'
                                        .trim()
                                    : '-',
                                offerPrimaryColor,
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 12),
                        Container(
                          padding: const EdgeInsets.all(10),
                          decoration: BoxDecoration(
                            color: offerPrimaryColor.withValues(alpha: 0.08),
                            borderRadius: BorderRadius.circular(12),
                            border: Border.all(
                              color: offerPrimaryColor.withValues(alpha: 0.22),
                            ),
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                offerSupplierName,
                                style: Theme.of(context)
                                    .textTheme
                                    .titleSmall
                                    ?.copyWith(
                                      fontWeight: FontWeight.w900,
                                      color: const Color(0xFF101828),
                                    ),
                              ),
                              const SizedBox(height: 4),
                              Text(
                                'Telephone: $offerSupplierPhone',
                                style: Theme.of(context).textTheme.bodyMedium,
                              ),
                              const SizedBox(height: 2),
                              Text(
                                'Email: $offerSupplierEmail',
                                style: Theme.of(context).textTheme.bodyMedium,
                              ),
                            ],
                          ),
                        ),
                        if (offerColors.isNotEmpty) ...[
                          const SizedBox(height: 10),
                          Wrap(
                            spacing: 8,
                            runSpacing: 8,
                            children: offerColors.entries.map((entry) {
                              final color = _colorForToken(entry.value);
                              return Container(
                                padding: const EdgeInsets.symmetric(
                                    horizontal: 8, vertical: 5),
                                decoration: BoxDecoration(
                                  color: color.withValues(alpha: 0.12),
                                  borderRadius: BorderRadius.circular(999),
                                  border: Border.all(
                                      color: color.withValues(alpha: 0.25)),
                                ),
                                child: Text(
                                  entry.key,
                                  style: TextStyle(
                                    fontSize: 12,
                                    fontWeight: FontWeight.w700,
                                    color: color,
                                  ),
                                ),
                              );
                            }).toList(),
                          ),
                        ],
                        if (hasInvoice) ...[
                          const SizedBox(height: 10),
                          OutlinedButton.icon(
                            onPressed: () => _openInvoiceFile(offer),
                            icon: const Icon(
                              Icons.picture_as_pdf_outlined,
                              size: 16,
                            ),
                            label: Text(
                              invoiceFile.isEmpty ? 'Voir facture' : 'Facture',
                            ),
                          ),
                        ],
                      ],
                    ),
                  );
                }).toList(),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _infoPill(
    String label,
    String value, {
    Color? backgroundColor,
    Color? borderColor,
    Color? textColor,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
      decoration: BoxDecoration(
        color: backgroundColor ??
            Theme.of(context)
                .colorScheme
                .surfaceContainerHighest
                .withValues(alpha: 0.45),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(
          color: borderColor ??
              Theme.of(context).colorScheme.outline.withValues(alpha: 0.20),
        ),
      ),
      child: Text(
        '$label: $value',
        style: Theme.of(context)
            .textTheme
            .bodySmall
            ?.copyWith(fontWeight: FontWeight.w600, color: textColor),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final rows = _filteredRows;
    return Column(
      children: [
        _buildHeader(),
        if (_loadingCampaigns) const LinearProgressIndicator(minHeight: 2),
        if (_loadingRows && rows.isNotEmpty)
          const LinearProgressIndicator(minHeight: 2),
        Expanded(
          child: (_loadingCampaigns || (_loadingRows && rows.isEmpty))
              ? const Center(child: CircularProgressIndicator())
              : (_error != null && _campaigns.isEmpty)
                  ? EmptyState(
                      icon: Icons.error_outline,
                      title: 'Unable to load strict campaigns',
                      message: _error!,
                      action: FilledButton(
                        onPressed: _loadCampaigns,
                        child: const Text('Retry'),
                      ),
                    )
                  : rows.isEmpty
                      ? EmptyState(
                          icon: Icons.rule_folder_outlined,
                          title: _cacheReady
                              ? 'No requested products to show'
                              : 'Analyse stricte en cours',
                          message: _selectedCampaignId == null
                              ? 'Select a campaign first.'
                              : (_cacheReady
                                  ? 'No products found for this campaign with current filters.'
                                  : 'Le cache strict est en preparation. Reessayez dans quelques instants.'),
                        )
                      : ListView.builder(
                          padding: const EdgeInsets.all(16),
                          itemCount: rows.length,
                          itemBuilder: (context, index) =>
                              _buildProductCard(rows[index]),
                        ),
        ),
      ],
    );
  }
}
