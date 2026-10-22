import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';

import '../api/campaigns_api.dart';
import '../api/client.dart';
import '../utils/file_opening.dart';
import '../widgets/empty_state.dart';

enum _BestMode {
  conformity,
  priceGpt70,
}

class BestOffersScreen extends StatefulWidget {
  const BestOffersScreen({super.key});

  @override
  State<BestOffersScreen> createState() => _BestOffersScreenState();
}

class _BestOffersScreenState extends State<BestOffersScreen> {
  static const double _minGptForPrice = 0.70;
  static const int _pageSize = 50;

  bool _loading = true;
  String? _error;
  List<dynamic> _campaigns = [];
  int? _selectedCampaignId;
  List<dynamic> _allProducts = [];
  List<Map<String, dynamic>> _rows = [];
  List<dynamic> _campaignAttachments = [];
  bool _loadingCampaignAttachments = false;
  final Set<int> _loadingTechDetailOfferIds = <int>{};
  _BestMode _mode = _BestMode.conformity;
  final TextEditingController _search = TextEditingController();
  String _query = '';

  @override
  void initState() {
    super.initState();
    _search.addListener(() {
      if (!mounted) return;
      setState(() => _query = _search.text.trim().toLowerCase());
    });
    _load();
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  double? _toDouble(dynamic value) {
    if (value is num) return value.toDouble();
    if (value is String) {
      return double.tryParse(value.trim().replaceAll(',', '.'));
    }
    return null;
  }

  double? _offerComparablePrice(dynamic offer) {
    final eq = _toDouble(offer['equivalent_unit_price']);
    if (eq != null && eq > 0) return eq;
    final direct = _toDouble(offer['unit_price']);
    if (direct != null && direct > 0) return direct;
    return null;
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

  void _showError(Object err) {
    if (!mounted) return;
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
      _showError(Exception("Impossible d'ouvrir le fichier"));
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
        _showError(err);
        return;
      }
    }

    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Fiche technique introuvable')),
    );
  }

  String _safeTempFileName(String preferred, String fallbackBase) {
    final raw = preferred.trim().isEmpty ? fallbackBase : preferred.trim();
    final safe = raw.replaceAll(RegExp(r'[<>:"/\\|?*]'), '_');
    return safe.isEmpty ? fallbackBase : safe;
  }

  Future<void> _openTempAnyFile({
    required List<int> bytes,
    required String preferredName,
    required String fallbackBase,
  }) async {
    final safeName = _safeTempFileName(preferredName, fallbackBase);
    final path =
        '${Directory.systemTemp.path}${Platform.pathSeparator}trial_${DateTime.now().millisecondsSinceEpoch}_$safeName';
    final out = File(path);
    await out.writeAsBytes(bytes, flush: true);
    await _openLocalFile(out.path);
  }

  Future<List<dynamic>> _loadCampaignAttachments(int campaignId) async {
    try {
      final rows = await CampaignsApi.attachments(campaignId);
      return List<dynamic>.from(rows);
    } catch (err) {
      _showError(err);
      return const <dynamic>[];
    }
  }

  String _fmtFileSize(dynamic raw) {
    final size = raw is num ? raw.toDouble() : double.tryParse('$raw');
    if (size == null || size <= 0) return '-';
    const units = ['B', 'KB', 'MB', 'GB'];
    var value = size;
    var idx = 0;
    while (value >= 1024 && idx < units.length - 1) {
      value /= 1024;
      idx += 1;
    }
    return '${value.toStringAsFixed(value >= 100 || idx == 0 ? 0 : 1)} ${units[idx]}';
  }

  Future<void> _openCampaignAttachment(
    int campaignId,
    dynamic rawAttachment,
  ) async {
    if (rawAttachment is! Map) return;
    final attachment = Map<String, dynamic>.from(rawAttachment);
    final filePath = (attachment['file_path'] ?? '').toString().trim();
    final fileName = (attachment['file_name'] ?? '').toString().trim().isEmpty
        ? 'attachment_${attachment['id'] ?? ''}'
        : (attachment['file_name'] ?? '').toString().trim();
    final attachmentIdRaw = attachment['id'];
    final attachmentId =
        attachmentIdRaw is num ? attachmentIdRaw.toInt() : null;

    if (filePath.isNotEmpty &&
        (_isLocalWindowsPath(filePath) ||
            filePath.startsWith('http://') ||
            filePath.startsWith('https://'))) {
      final opened = await _tryOpenLocalFile(filePath);
      if (opened) return;
    }

    if (attachmentId != null) {
      try {
        final bytes = await CampaignsApi.downloadCampaignAttachmentFile(
          campaignId,
          attachmentId,
        );
        await _openTempAnyFile(
          bytes: bytes,
          preferredName: fileName,
          fallbackBase: 'attachment_$attachmentId',
        );
        return;
      } catch (err) {
        _showError(err);
        return;
      }
    }

    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Fichier joint introuvable')),
    );
  }

  Future<void> _showCampaignAttachmentsDialog() async {
    final campaignId = _selectedCampaignId;
    if (campaignId == null) return;
    final attachments = List<dynamic>.from(_campaignAttachments);
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (context) {
        return AlertDialog(
          title: const Text('Fichiers joints campagne'),
          content: SizedBox(
            width: 760,
            height: 420,
            child: attachments.isEmpty
                ? const Center(child: Text('Aucun fichier joint'))
                : ListView.separated(
                    itemCount: attachments.length,
                    separatorBuilder: (_, __) => const Divider(height: 1),
                    itemBuilder: (context, index) {
                      final raw = attachments[index];
                      if (raw is! Map) return const SizedBox.shrink();
                      final row = Map<String, dynamic>.from(raw);
                      final name = (row['file_name'] ?? '').toString().trim();
                      final path = (row['file_path'] ?? '').toString().trim();
                      final score = _toDouble(row['parse_score']);
                      final engine =
                          (row['parse_engine'] ?? '').toString().trim();
                      final subtitle = <String>[
                        if (engine.isNotEmpty) 'engine: $engine',
                        if (score != null)
                          'score: ${(score * 100).toStringAsFixed(1)}%',
                        'taille: ${_fmtFileSize(row['file_size'])}',
                      ].join(' | ');
                      return ListTile(
                        dense: true,
                        title: Text(
                          name.isEmpty ? 'Fichier sans nom' : name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                        subtitle: Text(
                          subtitle,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                        ),
                        trailing: Wrap(
                          spacing: 8,
                          children: [
                            if (path.isNotEmpty)
                              Tooltip(
                                message: path,
                                child: const Icon(
                                  Icons.info_outline,
                                  size: 18,
                                  color: Color(0xFF486581),
                                ),
                              ),
                            OutlinedButton.icon(
                              onPressed: () =>
                                  _openCampaignAttachment(campaignId, row),
                              icon: const Icon(Icons.open_in_new, size: 16),
                              label: const Text('Ouvrir'),
                            ),
                          ],
                        ),
                      );
                    },
                  ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('Fermer'),
            ),
          ],
        );
      },
    );
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
        return {'score': descriptionScore};
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

  List<String> _collectTextLines(dynamic raw) {
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
          final text = (item['text'] ?? item['value'] ?? '').toString().trim();
          if (text.isNotEmpty) out.add(text);
        }
      }
    }
    return out;
  }

  List<String> _componentLinesFromPayload(dynamic payload, String key) {
    if (payload is! Map) return const [];
    if (key == 'description') {
      final desc = payload['description'];
      return _collectTextLines(desc);
    }
    return _collectTextLines(payload[key]);
  }

  List<Map<String, dynamic>> _componentDetailRows(dynamic component) {
    if (component is! Map) return const [];
    final rows = component['details'];
    if (rows is! List) return const [];
    final out = <Map<String, dynamic>>[];
    for (final row in rows) {
      if (row is Map) {
        out.add(Map<String, dynamic>.from(row));
      }
    }
    return out;
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
              style: const TextStyle(
                fontSize: 12,
                color: Color(0xFF102A43),
              ),
            ),
            const SizedBox(height: 2),
            Text(
              'Offert: ${offeredText.isEmpty ? "-" : offeredText}',
              style: const TextStyle(
                fontSize: 12,
                color: Color(0xFF334E68),
              ),
            ),
          ],
        ),
      );
    }

    Widget componentRow(String key, String title) {
      final raw = components[key];
      final map = raw is Map ? raw : const {};
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
        if (totalInt > 0) {
          coverageText = _fmtPercent(matchedInt / totalInt);
        }
      } else {
        final coverage = _toDouble(map['coverage']);
        if (coverage != null) {
          coverageText = _fmtPercent(coverage);
        }
      }
      final detailRows = _componentDetailRows(map);
      final matchedRows =
          detailRows.where((row) => row['matched'] == true).toList();
      final unmatchedRows =
          detailRows.where((row) => row['matched'] != true).toList();
      final requestedLines =
          _componentLinesFromPayload(breakdown['requested'], key);
      final offeredLines =
          _componentLinesFromPayload(breakdown['offered'], key);
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
            Text(
              title,
              style: const TextStyle(
                fontWeight: FontWeight.w700,
                fontSize: 14,
                color: Color(0xFF102A43),
              ),
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                metricPill(
                  icon: Icons.checklist_rtl_outlined,
                  label: 'Couverture',
                  value: '$ratio ($coverageText)',
                  color: const Color(0xFF1D4ED8),
                ),
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
                  title: const Text('Voir dÃƒÂ©tails'),
                  subtitle: Text(
                    detailRows.isNotEmpty
                        ? '${matchedRows.length} matchÃƒÂ©s | ${unmatchedRows.length} non matchÃƒÂ©s'
                        : 'DÃƒÂ©tails textuels',
                  ),
                  children: [
                    if (detailRows.isNotEmpty) ...[
                      if (matchedRows.isNotEmpty) ...[
                        const Align(
                          alignment: Alignment.centerLeft,
                          child: Text(
                            'MatchÃƒÂ©s',
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
                            'Non matchÃƒÂ©s',
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
        ? '${debugJson.substring(0, 12000)}\n... [tronquÃƒÂ©]'
        : debugJson;

    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Details conformite (GPT)'),
        content: SizedBox(
          width: 820,
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
                        'Produit demande: $requestedName',
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
                            label: 'Score global',
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
                    child: const Text(
                      'Details GPT non disponibles pour cette offre.',
                      style: TextStyle(color: Color(0xFF334E68)),
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
                    subtitle: const Text('JSON GPT + mÃƒÂ©tadonnÃƒÂ©es'),
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

  Future<void> _openTechSimilarityDetails({
    required int productId,
    required String requestedName,
    required Map<String, dynamic> offer,
  }) async {
    final campaignId = _selectedCampaignId;
    if (campaignId == null) return;
    final invoiceItemIdRaw = offer['invoice_item_id'];
    final detailOfferId =
        invoiceItemIdRaw is num ? invoiceItemIdRaw.toInt() : null;
    if (detailOfferId != null && detailOfferId > 0) {
      if (_loadingTechDetailOfferIds.contains(detailOfferId)) {
        return;
      }
      setState(() => _loadingTechDetailOfferIds.add(detailOfferId));
    }
    dynamic effectiveOffer = offer;
    final hasBreakdown = _techBreakdownMap(offer) != null;
    try {
      if (!hasBreakdown &&
          invoiceItemIdRaw is num &&
          invoiceItemIdRaw.toInt() > 0) {
        final detail = await CampaignsApi.offerByProductDetail(
          campaignId,
          productId,
          invoiceItemIdRaw.toInt(),
        );
        effectiveOffer = {
          ...offer,
          ...detail,
        };
      }
      if (!mounted) return;
      await _showTechSimilarityDialog(
        requestedName: requestedName,
        offer: effectiveOffer,
      );
    } catch (err) {
      _showError(err);
    } finally {
      if (detailOfferId != null && detailOfferId > 0 && mounted) {
        setState(() => _loadingTechDetailOfferIds.remove(detailOfferId));
      }
    }
  }

  double? _gpt(dynamic offer) {
    final direct = _toDouble(offer['gpt_match_rate']);
    if (direct != null) return direct.clamp(0.0, 1.0).toDouble();
    final fallback = _toDouble(offer['embedding_match_rate']);
    if (fallback != null) return fallback.clamp(0.0, 1.0).toDouble();
    return null;
  }

  double? _tech(dynamic offer) {
    final display = _toDouble(offer['tech_sheet_similarity_display_score']);
    if (display != null) return display.clamp(0.0, 1.0).toDouble();
    final raw = _toDouble(offer['tech_sheet_similarity_score']);
    if (raw != null) return raw.clamp(0.0, 1.0).toDouble();
    return null;
  }

  Color _tone(double? score) {
    if (score == null) return const Color(0xFF486581);
    if (score >= 0.80) return const Color(0xFF0F766E);
    if (score >= 0.60) return const Color(0xFFD97706);
    return const Color(0xFF6B7280);
  }

  String _emptyReasonLabel(String reason) {
    switch (reason) {
      case 'no_offers':
        return 'Aucune offre recue';
      case 'no_tech_candidate':
        return 'Aucune offre avec conformite calculee';
      case 'no_gpt70_candidate':
        return 'Aucune offre avec score GPT >= 70%';
      default:
        return 'Aucune offre retenue';
    }
  }

  Widget _chip({
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
        border: Border.all(color: color.withValues(alpha: 0.32)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 14, color: color),
          const SizedBox(width: 4),
          Text(
            '$label: $value',
            style: TextStyle(
                fontSize: 12, fontWeight: FontWeight.w600, color: color),
          ),
        ],
      ),
    );
  }

  Future<List<dynamic>> _fetchAllProducts(int campaignId) async {
    final out = <dynamic>[];
    int page = 1;
    while (true) {
      final payload = await CampaignsApi.offersByProduct(
        campaignId,
        conformOnly: false,
        page: page,
        pageSize: _pageSize,
      );
      out.addAll(List<dynamic>.from((payload['items'] as List?) ?? const []));
      if (payload['has_more'] != true) break;
      page += 1;
      if (page > 500) break;
    }
    return out;
  }

  Future<List<dynamic>> _loadProductsForCampaign(int campaignId) async {
    final offeredProducts = await _fetchAllProducts(campaignId);
    final requestedItems = await CampaignsApi.items(campaignId);

    final byId = <int, Map<String, dynamic>>{};

    for (final raw in offeredProducts) {
      if (raw is! Map) continue;
      final product = Map<String, dynamic>.from(raw);
      final pidRaw = product['product_id'];
      if (pidRaw is! num) continue;
      byId[pidRaw.toInt()] = {
        ...product,
        'product_id': pidRaw.toInt(),
        'product_name': (product['product_name'] ?? '').toString(),
        'requested_qty': _toDouble(product['requested_qty']),
        'offers': List<dynamic>.from((product['offers'] as List?) ?? const []),
      };
    }

    for (final raw in requestedItems) {
      if (raw is! Map) continue;
      final item = Map<String, dynamic>.from(raw);
      final pidRaw = item['product_id'];
      if (pidRaw is! num) continue;
      final pid = pidRaw.toInt();
      final name = (item['product_name'] ?? item['name'] ?? '').toString();
      final requestedQty = _toDouble(item['requested_qty']);
      final current = byId[pid];
      if (current == null) {
        byId[pid] = {
          'product_id': pid,
          'product_name': name,
          'requested_qty': requestedQty,
          'offers': const <dynamic>[],
        };
      } else {
        final currentName = (current['product_name'] ?? '').toString().trim();
        if (currentName.isEmpty && name.trim().isNotEmpty) {
          current['product_name'] = name;
        }
        final currentQty = _toDouble(current['requested_qty']);
        if (currentQty == null && requestedQty != null && requestedQty > 0) {
          current['requested_qty'] = requestedQty;
        }
      }
    }

    return byId.values.toList();
  }

  Map<String, dynamic>? _bestForProduct(List<dynamic> offers) {
    if (_mode == _BestMode.conformity) {
      Map<String, dynamic>? best;
      double bestScore = -1;
      double bestPrice = double.infinity;
      for (final raw in offers) {
        if (raw is! Map) continue;
        final offer = Map<String, dynamic>.from(raw);
        final score = _tech(offer);
        if (score == null) continue;
        final price = _offerComparablePrice(offer) ?? double.infinity;
        if (best == null ||
            score > bestScore + 1e-9 ||
            ((score - bestScore).abs() <= 1e-9 && price < bestPrice)) {
          best = offer;
          bestScore = score;
          bestPrice = price;
        }
      }
      return best;
    }

    Map<String, dynamic>? best;
    double bestPrice = double.infinity;
    double bestGpt = -1;
    for (final raw in offers) {
      if (raw is! Map) continue;
      final offer = Map<String, dynamic>.from(raw);
      final gpt = _toDouble(offer['gpt_match_rate']);
      if (gpt == null || gpt < _minGptForPrice) continue;
      final price = _offerComparablePrice(offer);
      if (price == null) continue;
      if (price <= 0) continue;
      if (best == null ||
          price < bestPrice - 1e-9 ||
          ((price - bestPrice).abs() <= 1e-9 && gpt > bestGpt)) {
        best = offer;
        bestPrice = price;
        bestGpt = gpt;
      }
    }
    return best;
  }

  void _computeRows() {
    final out = <Map<String, dynamic>>[];
    for (final raw in _allProducts) {
      if (raw is! Map) continue;
      final product = Map<String, dynamic>.from(raw);
      final id = product['product_id'];
      final name = (product['product_name'] ?? '').toString().trim();
      final requestedQty = _toDouble(product['requested_qty']);
      final offers =
          List<dynamic>.from((product['offers'] as List?) ?? const []);
      if (id is! num || name.isEmpty) continue;
      final best = _bestForProduct(offers);
      final hasBest = best != null;
      int eligible = 0;
      if (_mode == _BestMode.conformity) {
        for (final o in offers) {
          if (_tech(o) != null) eligible += 1;
        }
      } else {
        for (final o in offers) {
          final gpt = _toDouble((o as Map)['gpt_match_rate']);
          if (gpt != null && gpt >= _minGptForPrice) eligible += 1;
        }
      }
      out.add({
        'product_id': id.toInt(),
        'product_name': name,
        'requested_qty': requestedQty,
        'offers_count': offers.length,
        'eligible_count': eligible,
        'has_best': hasBest,
        'empty_reason': offers.isEmpty
            ? 'no_offers'
            : (_mode == _BestMode.conformity
                ? 'no_tech_candidate'
                : 'no_gpt70_candidate'),
        'offer': hasBest ? best : const <String, dynamic>{},
      });
    }
    out.sort((a, b) {
      final aHasBest = a['has_best'] == true;
      final bHasBest = b['has_best'] == true;
      if (aHasBest != bHasBest) {
        return bHasBest ? 1 : -1;
      }
      if (_mode == _BestMode.conformity) {
        final as = _tech(a['offer']) ?? -1;
        final bs = _tech(b['offer']) ?? -1;
        final cmp = bs.compareTo(as);
        if (cmp != 0) return cmp;
      } else {
        final ap = _offerComparablePrice(a['offer']) ?? double.infinity;
        final bp = _offerComparablePrice(b['offer']) ?? double.infinity;
        final cmp = ap.compareTo(bp);
        if (cmp != 0) return cmp;
      }
      return (a['product_name'] as String)
          .toLowerCase()
          .compareTo((b['product_name'] as String).toLowerCase());
    });
    _rows = out;
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _loadingCampaignAttachments = true;
      _error = null;
    });
    try {
      final campaigns = await CampaignsApi.list();
      int? selected = _selectedCampaignId;
      if (campaigns.isEmpty) {
        selected = null;
      } else if (selected == null ||
          !campaigns.any((c) => c['id'] == selected)) {
        selected = campaigns.first['id'] as int?;
      }

      List<dynamic> products = [];
      List<dynamic> attachments = const <dynamic>[];
      if (selected != null) {
        products = await _loadProductsForCampaign(selected);
        attachments = await _loadCampaignAttachments(selected);
      }
      if (!mounted) return;
      setState(() {
        _campaigns = campaigns;
        _selectedCampaignId = selected;
        _allProducts = products;
        _campaignAttachments = attachments;
        _computeRows();
        _loading = false;
        _loadingCampaignAttachments = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = ApiClient.formatError(err);
        _loading = false;
        _loadingCampaignAttachments = false;
      });
    }
  }

  Future<void> _onCampaignChanged(int? campaignId) async {
    if (campaignId == null || campaignId == _selectedCampaignId) return;
    setState(() {
      _selectedCampaignId = campaignId;
      _loading = true;
      _loadingCampaignAttachments = true;
      _error = null;
    });
    try {
      final products = await _loadProductsForCampaign(campaignId);
      final attachments = await _loadCampaignAttachments(campaignId);
      if (!mounted) return;
      setState(() {
        _allProducts = products;
        _campaignAttachments = attachments;
        _computeRows();
        _loading = false;
        _loadingCampaignAttachments = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = ApiClient.formatError(err);
        _loading = false;
        _loadingCampaignAttachments = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(_error!, style: const TextStyle(color: Colors.red)),
            const SizedBox(height: 8),
            ElevatedButton(onPressed: _load, child: const Text('Retry')),
          ],
        ),
      );
    }
    if (_campaigns.isEmpty) {
      return const EmptyState(
        icon: Icons.workspace_premium_outlined,
        title: 'Aucune campagne',
        message: 'Cree une campagne puis lance extraction/comparaison.',
      );
    }

    final filtered = _rows.where((row) {
      if (_query.isEmpty) return true;
      final product = (row['product_name'] ?? '').toString().toLowerCase();
      final offer =
          Map<String, dynamic>.from((row['offer'] as Map?) ?? const {});
      final supplier = (offer['supplier_name'] ?? '').toString().toLowerCase();
      final extracted =
          (offer['extracted_name'] ?? '').toString().toLowerCase();
      return product.contains(_query) ||
          supplier.contains(_query) ||
          extracted.contains(_query);
    }).toList();

    final title = _mode == _BestMode.conformity
        ? 'Meilleure conformite globale'
        : 'Meilleur prix (GPT >= 70%)';
    final selectedCount = _rows.where((row) => row['has_best'] == true).length;
    final attachmentsCount = _campaignAttachments.length;

    return Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 8,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              const Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.workspace_premium_outlined, size: 20),
                  SizedBox(width: 8),
                  Text(
                    'Top Offres',
                    style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                  ),
                ],
              ),
              OutlinedButton.icon(
                onPressed:
                    _selectedCampaignId == null || _loadingCampaignAttachments
                        ? null
                        : _showCampaignAttachmentsDialog,
                icon: _loadingCampaignAttachments
                    ? const SizedBox(
                        width: 14,
                        height: 14,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.attach_file, size: 16),
                label: Text('Fichiers joints ($attachmentsCount)'),
              ),
              IconButton(onPressed: _load, icon: const Icon(Icons.refresh)),
            ],
          ),
          const SizedBox(height: 12),
          LayoutBuilder(
            builder: (context, constraints) {
              final maxWidth = constraints.maxWidth;
              final campaignFieldWidth =
                  (maxWidth < 380 ? maxWidth : 360.0).toDouble();
              final searchFieldWidth =
                  (maxWidth < 340 ? maxWidth : 320.0).toDouble();
              return Wrap(
                spacing: 12,
                runSpacing: 10,
                children: [
                  SizedBox(
                    width: campaignFieldWidth,
                    child: DropdownButtonFormField<int>(
                      value: _selectedCampaignId,
                      isExpanded: true,
                      decoration: const InputDecoration(
                        labelText: 'Campagne',
                        border: OutlineInputBorder(),
                      ),
                      selectedItemBuilder: (context) => _campaigns
                          .map(
                            (c) => Align(
                              alignment: Alignment.centerLeft,
                              child: Text(
                                '${c['id']} - ${c['title']}',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                          )
                          .toList(),
                      items: _campaigns
                          .map((c) => DropdownMenuItem<int>(
                                value: c['id'] as int,
                                child: Text(
                                  '${c['id']} - ${c['title']}',
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                ),
                              ))
                          .toList(),
                      onChanged: _onCampaignChanged,
                    ),
                  ),
                  ChoiceChip(
                    label: const Text('Best conformite'),
                    selected: _mode == _BestMode.conformity,
                    onSelected: (v) {
                      if (!v) return;
                      setState(() {
                        _mode = _BestMode.conformity;
                        _computeRows();
                      });
                    },
                  ),
                  ChoiceChip(
                    label: const Text('Best prix (GPT>=70%)'),
                    selected: _mode == _BestMode.priceGpt70,
                    onSelected: (v) {
                      if (!v) return;
                      setState(() {
                        _mode = _BestMode.priceGpt70;
                        _computeRows();
                      });
                    },
                  ),
                  SizedBox(
                    width: searchFieldWidth,
                    child: TextField(
                      controller: _search,
                      decoration: const InputDecoration(
                        hintText: 'Rechercher produit/fournisseur...',
                        border: OutlineInputBorder(),
                        prefixIcon: Icon(Icons.search),
                        isDense: true,
                      ),
                    ),
                  ),
                ],
              );
            },
          ),
          const SizedBox(height: 10),
          Text(
            '$title | ${filtered.length} produits affiches ($selectedCount avec offre retenue) | $attachmentsCount fichiers joints',
            style: const TextStyle(
                fontSize: 13,
                color: Color(0xFF486581),
                fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 10),
          Expanded(
            child: filtered.isEmpty
                ? const EmptyState(
                    icon: Icons.filter_alt_off_outlined,
                    title: 'Aucun produit eligible',
                    message: 'Aucun produit ne correspond au mode choisi.',
                  )
                : ListView.builder(
                    itemCount: filtered.length,
                    itemBuilder: (context, index) {
                      final row = filtered[index];
                      final hasBest = row['has_best'] == true;
                      final emptyReason =
                          (row['empty_reason'] ?? '').toString().trim();
                      final offer = Map<String, dynamic>.from(
                          (row['offer'] as Map?) ?? const {});
                      final requestedQty = _toDouble(row['requested_qty']) ??
                          _toDouble(offer['requested_qty']);
                      final requestedName =
                          (row['product_name'] ?? '').toString();
                      final extractedName =
                          (offer['extracted_name'] ?? '').toString().trim();
                      final extractedDesc =
                          (offer['extracted_description'] ?? '')
                              .toString()
                              .trim();
                      final supplier =
                          (offer['supplier_name'] ?? '').toString();
                      final unitPrice = _toDouble(offer['unit_price']);
                      final equivalentPrice =
                          _toDouble(offer['equivalent_unit_price']);
                      final comparablePrice = _offerComparablePrice(offer);
                      final eqBasisRaw =
                          (offer['equivalence_basis'] ?? '').toString().trim();
                      final isNonConvertible =
                          eqBasisRaw.toLowerCase().startsWith('direct_');
                      final totalRequestedComparable = hasBest &&
                              requestedQty != null &&
                              comparablePrice != null &&
                              requestedQty > 0
                          ? (requestedQty * comparablePrice)
                          : null;
                      final totalRequestedReason = !hasBest
                          ? null
                          : (requestedQty == null || requestedQty <= 0)
                                  ? 'qte demandee manquante'
                                  : (comparablePrice == null)
                                      ? (isNonConvertible
                                          ? 'PU manquant'
                                          : 'PU compare manquant')
                                      : 'non calculable';
                      final eqFactor = _toDouble(offer['equivalence_factor']);
                      final eqConfidence =
                          _toDouble(offer['equivalence_confidence']);
                      final currency = (offer['currency'] ?? 'DZD').toString();
                      final eqBasis = eqBasisRaw.isEmpty
                          ? ''
                          : (eqBasisRaw.length > 48
                              ? '${eqBasisRaw.substring(0, 48)}...'
                              : eqBasisRaw);
                      final conversionLabel = !hasBest
                          ? '-'
                          : isNonConvertible
                              ? 'Non converti'
                              : (eqFactor != null
                                  ? 'Converti x${_fmtValue(eqFactor, decimals: 4)}'
                                  : 'Conversion inconnue');
                      final unitPriceLabel = unitPrice == null
                          ? '-'
                          : '${_fmtValue(unitPrice, decimals: 2)} $currency';
                      final equivalentPriceLabel = (!isNonConvertible &&
                              equivalentPrice != null)
                          ? '${_fmtValue(equivalentPrice, decimals: 2)} $currency'
                          : 'N/A';
                      final gpt = _gpt(offer);
                      final tech = _tech(offer);
                      final techStatusRaw =
                          (offer['tech_sheet_similarity_display_status'] ??
                                  offer['tech_sheet_similarity_status'] ??
                                  '')
                              .toString()
                              .trim();
                      final tone = hasBest
                          ? _tone(tech ?? gpt)
                          : const Color(0xFF6B7280);

                      final techCharRatio =
                          _techComponentRatio(offer, 'characteristics');
                      final techCharPercent =
                          _techComponentPercent(offer, 'characteristics');
                      final techReqRatio =
                          _techComponentRatio(offer, 'requirements');
                      final techReqPercent =
                          _techComponentPercent(offer, 'requirements');
                      final techAccRatio =
                          _techComponentRatio(offer, 'accessories');
                      final techAccPercent =
                          _techComponentPercent(offer, 'accessories');
                      final techDescPercent =
                          _techComponentPercent(offer, 'description');

                      final invoicePath =
                          (offer['invoice_file_path'] ?? '').toString().trim();
                      final invoiceFile =
                          (offer['invoice_file_name'] ?? '').toString().trim();
                      final invoiceIdRaw = offer['invoice_id'];
                      final invoiceId =
                          invoiceIdRaw is num ? invoiceIdRaw.toInt() : null;
                      final techPath = (offer['tech_sheet_file_path'] ?? '')
                          .toString()
                          .trim();
                      final techFile =
                          (offer['tech_sheet_file'] ?? '').toString().trim();
                      final techIdRaw = offer['tech_sheet_id'];
                      final techId =
                          techIdRaw is num ? techIdRaw.toInt() : null;
                      final invoiceItemIdRaw = offer['invoice_item_id'];
                      final detailOfferId = invoiceItemIdRaw is num
                          ? invoiceItemIdRaw.toInt()
                          : null;
                      final detailLoading = detailOfferId != null &&
                          _loadingTechDetailOfferIds.contains(detailOfferId);
                      final hasTechDetails = _techBreakdownMap(offer) != null;
                      final hasInvoice =
                          invoicePath.isNotEmpty || invoiceId != null;
                      final hasTech = techPath.isNotEmpty || techId != null;
                      final hasAdvancedDetails = hasBest &&
                          (techStatusRaw.isNotEmpty ||
                              techCharRatio != null ||
                              techCharPercent != null ||
                              techReqRatio != null ||
                              techReqPercent != null ||
                              techAccRatio != null ||
                              techAccPercent != null ||
                              techDescPercent != null ||
                              (!isNonConvertible && equivalentPrice != null) ||
                              (!isNonConvertible && eqFactor != null) ||
                              (!isNonConvertible && eqConfidence != null) ||
                              (!isNonConvertible && eqBasis.isNotEmpty) ||
                              (offer['price_source'] ?? '')
                                  .toString()
                                  .trim()
                                  .isNotEmpty ||
                              (equivalentPrice != null &&
                                  unitPrice != null &&
                                  (equivalentPrice - unitPrice).abs() > 1e-9));

                      return Container(
                        margin: const EdgeInsets.only(bottom: 12),
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                          color: tone.withValues(alpha: 0.08),
                          borderRadius: BorderRadius.circular(10),
                          border:
                              Border.all(color: tone.withValues(alpha: 0.35)),
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Row(
                              children: [
                                const Icon(Icons.inventory_2_outlined,
                                    size: 16),
                                const SizedBox(width: 6),
                                Expanded(
                                  child: Text(
                                    requestedName,
                                    style: const TextStyle(
                                        fontWeight: FontWeight.w700,
                                        fontSize: 14),
                                  ),
                                ),
                                Text(
                                  'Eligibles: ${row['eligible_count']} / ${row['offers_count']}',
                                  style: const TextStyle(
                                      fontSize: 12, color: Color(0xFF486581)),
                                ),
                              ],
                            ),
                            const SizedBox(height: 6),
                            if (hasBest)
                              Text(
                                supplier.isEmpty
                                    ? 'Fournisseur inconnu'
                                    : supplier,
                                style: const TextStyle(
                                    fontWeight: FontWeight.w600, fontSize: 13),
                              )
                            else
                              Text(
                                _emptyReasonLabel(emptyReason),
                                style: const TextStyle(
                                  fontWeight: FontWeight.w600,
                                  fontSize: 13,
                                  color: Color(0xFF6B7280),
                                ),
                              ),
                            const SizedBox(height: 6),
                            Text(
                              extractedName.isEmpty
                                  ? requestedName
                                  : extractedName,
                              style: const TextStyle(
                                fontSize: 13,
                                fontWeight: FontWeight.w700,
                                color: Color(0xFF243B53),
                              ),
                            ),
                            if (extractedDesc.isNotEmpty) ...[
                              const SizedBox(height: 3),
                              Text(
                                extractedDesc,
                                maxLines: 3,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                    fontSize: 12, color: Color(0xFF486581)),
                              ),
                            ],
                            const SizedBox(height: 8),
                            if (hasBest)
                              Text(
                                'Qte demandee: ${_fmtValue(requestedQty)} | Qte facture: ${_fmtValue(offer['offered_qty'])} | PU facture: $unitPriceLabel | PU eq.: $equivalentPriceLabel',
                                style: const TextStyle(
                                    fontSize: 12, fontWeight: FontWeight.w600),
                              )
                            else
                              const Text(
                                'Qte: - | PU: -',
                                style: TextStyle(
                                    fontSize: 12, fontWeight: FontWeight.w600),
                              ),
                            const SizedBox(height: 8),
                            Wrap(
                              spacing: 8,
                              runSpacing: 8,
                              children: [
                                if (hasBest && gpt != null)
                                  _chip(
                                    icon: Icons.smart_toy_outlined,
                                    label: 'Score GPT',
                                    value: _fmtPercent(gpt),
                                    color: tone,
                                  ),
                                if (hasBest)
                                  _chip(
                                    icon: Icons.local_offer_outlined,
                                    label: 'PU facture',
                                    value: unitPriceLabel,
                                    color: const Color(0xFF1B6CA8),
                                  ),
                                if (hasBest)
                                  _chip(
                                    icon: Icons.price_change_outlined,
                                    label: 'PU eq.',
                                    value: equivalentPriceLabel,
                                    color: const Color(0xFF0F766E),
                                  ),
                                if (hasBest)
                                  _chip(
                                    icon: Icons.swap_horiz_outlined,
                                    label: 'Conversion',
                                    value: conversionLabel,
                                    color: const Color(0xFF334E68),
                                  ),
                                if (hasBest)
                                  _chip(
                                    icon: Icons.calculate_outlined,
                                    label: 'Total demande',
                                    value: totalRequestedComparable != null
                                        ? '${_fmtValue(totalRequestedComparable)} ${(offer['currency'] ?? 'DZD')}'
                                        : 'N/A (${totalRequestedReason ?? '-'})',
                                    color: const Color(0xFF1B6CA8),
                                  ),
                                if (hasBest &&
                                    !isNonConvertible &&
                                    eqFactor != null)
                                  _chip(
                                    icon: Icons.exposure_plus_1_outlined,
                                    label: 'Facteur eq.',
                                    value: _fmtValue(eqFactor, decimals: 4),
                                    color: const Color(0xFF0F766E),
                                  ),
                                if (hasBest &&
                                    !isNonConvertible &&
                                    eqConfidence != null)
                                  _chip(
                                    icon: Icons.rule_folder_outlined,
                                    label: 'Confiance eq.',
                                    value: _fmtPercent(eqConfidence),
                                    color: const Color(0xFF486581),
                                  ),
                                if (hasBest &&
                                    !isNonConvertible &&
                                    eqBasis.isNotEmpty)
                                  _chip(
                                    icon: Icons.tune_outlined,
                                    label: 'Base eq.',
                                    value: eqBasis,
                                    color: const Color(0xFF334E68),
                                  ),
                                _chip(
                                  icon: Icons.verified_outlined,
                                  label: 'Conformite globale',
                                  value: hasBest && tech != null
                                      ? _fmtPercent(tech)
                                      : '-',
                                  color: const Color(0xFF0F766E),
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
                                        if (hasBest &&
                                            !isNonConvertible &&
                                            equivalentPrice != null)
                                          _chip(
                                            icon: Icons.price_change_outlined,
                                            label: 'PU eq.',
                                            value:
                                                '${_fmtValue(equivalentPrice)} ${(offer['currency'] ?? 'DZD')}',
                                            color: const Color(0xFF0F766E),
                                          ),
                                        if (hasBest &&
                                            !isNonConvertible &&
                                            eqFactor != null)
                                          _chip(
                                            icon:
                                                Icons.exposure_plus_1_outlined,
                                            label: 'Facteur eq.',
                                            value: _fmtValue(eqFactor,
                                                decimals: 4),
                                            color: const Color(0xFF0F766E),
                                          ),
                                        if (hasBest &&
                                            !isNonConvertible &&
                                            eqConfidence != null)
                                          _chip(
                                            icon: Icons.rule_folder_outlined,
                                            label: 'Confiance eq.',
                                            value: _fmtPercent(eqConfidence),
                                            color: const Color(0xFF486581),
                                          ),
                                        if (hasBest &&
                                            !isNonConvertible &&
                                            eqBasis.isNotEmpty)
                                          _chip(
                                            icon: Icons.tune_outlined,
                                            label: 'Base eq.',
                                            value: eqBasis,
                                            color: const Color(0xFF334E68),
                                          ),
                                        _chip(
                                          icon: Icons
                                              .assignment_turned_in_outlined,
                                          label: 'Statut fiche',
                                          value: hasBest &&
                                                  techStatusRaw.isNotEmpty
                                              ? _techSimilarityStatusLabel(
                                                  techStatusRaw)
                                              : '-',
                                          color: const Color(0xFF4C6FFF),
                                        ),
                                        _chip(
                                          icon: Icons.checklist_rtl_outlined,
                                          label: 'Caract. fiche',
                                          value: hasBest
                                              ? _ratioWithPercent(
                                                  techCharRatio,
                                                  techCharPercent,
                                                )
                                              : '-',
                                          color: const Color(0xFF4C6FFF),
                                        ),
                                        _chip(
                                          icon: Icons.assignment_outlined,
                                          label: 'Exigences',
                                          value: hasBest
                                              ? _ratioWithPercent(
                                                  techReqRatio,
                                                  techReqPercent,
                                                )
                                              : '-',
                                          color: const Color(0xFF7C3AED),
                                        ),
                                        _chip(
                                          icon: Icons
                                              .precision_manufacturing_outlined,
                                          label: 'Accessoires',
                                          value: hasBest
                                              ? _ratioWithPercent(
                                                  techAccRatio,
                                                  techAccPercent,
                                                )
                                              : '-',
                                          color: const Color(0xFF1D4ED8),
                                        ),
                                        _chip(
                                          icon: Icons.description_outlined,
                                          label: 'Description',
                                          value: hasBest
                                              ? (techDescPercent ?? '-')
                                              : '-',
                                          color: const Color(0xFF0F766E),
                                        ),
                                        _chip(
                                          icon: Icons.payments_outlined,
                                          label: 'Source prix',
                                          value: hasBest &&
                                                  (offer['price_source'] ?? '')
                                                      .toString()
                                                      .trim()
                                                      .isNotEmpty
                                              ? (offer['price_source'] ?? '')
                                                  .toString()
                                              : '-',
                                          color: const Color(0xFF1B6CA8),
                                        ),
                                      ],
                                    ),
                                    if (hasBest &&
                                        equivalentPrice != null &&
                                        unitPrice != null &&
                                        (equivalentPrice - unitPrice).abs() >
                                            1e-9) ...[
                                      const SizedBox(height: 8),
                                      Text(
                                        'PU brut: ${_fmtValue(unitPrice)} ${(offer['currency'] ?? 'DZD')}',
                                        style: const TextStyle(
                                          fontSize: 12,
                                          color: Color(0xFF486581),
                                          fontWeight: FontWeight.w600,
                                        ),
                                      ),
                                    ],
                                  ],
                                ),
                              ),
                            ],
                            if (hasBest &&
                                (hasInvoice ||
                                    hasTech ||
                                    hasTechDetails ||
                                    techStatusRaw.isNotEmpty)) ...[
                              const SizedBox(height: 8),
                              Wrap(
                                spacing: 8,
                                runSpacing: 8,
                                children: [
                                  if (hasTechDetails ||
                                      techStatusRaw.isNotEmpty)
                                    OutlinedButton.icon(
                                      onPressed: detailLoading
                                          ? null
                                          : () => _openTechSimilarityDetails(
                                                productId:
                                                    row['product_id'] as int,
                                                requestedName: requestedName,
                                                offer: offer,
                                              ),
                                      icon: detailLoading
                                          ? const SizedBox(
                                              width: 16,
                                              height: 16,
                                              child: CircularProgressIndicator(
                                                  strokeWidth: 2),
                                            )
                                          : const Icon(Icons.policy_outlined,
                                              size: 16),
                                      label: Text(detailLoading
                                          ? 'Chargement...'
                                          : 'Details conformite'),
                                    ),
                                  if (hasInvoice)
                                    OutlinedButton.icon(
                                      onPressed: () => _openInvoiceFile(offer),
                                      icon: const Icon(
                                          Icons.picture_as_pdf_outlined,
                                          size: 16),
                                      label: Text(invoiceFile.isEmpty
                                          ? 'Voir facture'
                                          : 'Facture'),
                                    ),
                                  if (hasTech)
                                    OutlinedButton.icon(
                                      onPressed: () =>
                                          _openTechSheetFile(offer),
                                      icon: const Icon(
                                          Icons.description_outlined,
                                          size: 16),
                                      label: Text(techFile.isEmpty
                                          ? 'Voir fiche'
                                          : 'Fiche technique'),
                                    ),
                                ],
                              ),
                            ],
                          ],
                        ),
                      );
                    },
                  ),
          ),
        ],
      ),
    );
  }
}
