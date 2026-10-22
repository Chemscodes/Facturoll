import 'package:flutter/material.dart';

import '../api/campaigns_api.dart';
import '../api/client.dart';
import '../api/invoice_extractions_api.dart';
import '../config/app_thresholds.dart';
import '../utils/file_opening.dart';
import '../widgets/empty_state.dart';

class VerificationScreen extends StatefulWidget {
  const VerificationScreen({super.key});

  @override
  State<VerificationScreen> createState() => _VerificationScreenState();
}

class _VerificationScreenState extends State<VerificationScreen> {
  bool _loading = true;
  String? _error;
  List<dynamic> _items = [];
  List<dynamic> _campaigns = [];
  List<dynamic> _invoices = [];
  int? _selectedCampaignId;
  int? _selectedInvoiceId;
  bool _simpleView = true;

  Future<bool> _openWithShell(String path) async {
    return openWithShell(path);
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
    await _openFilePath(path);
  }

  Future<void> _openInvoiceFile({
    required int invoiceId,
    required String localPath,
    required String preferredName,
  }) async {
    if (localPath.isNotEmpty &&
        (_isLocalWindowsPath(localPath) ||
            localPath.startsWith('http://') ||
            localPath.startsWith('https://'))) {
      final opened = await _openWithShell(localPath);
      if (opened) {
        return;
      }
    }
    try {
      final bytes = await CampaignsApi.downloadInvoiceFile(invoiceId);
      await _openTempPdf(
        bytes: bytes,
        preferredName: preferredName,
        fallbackBase: 'invoice_$invoiceId',
      );
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      _campaigns = await CampaignsApi.list();
      if (_selectedCampaignId != null &&
          !_campaigns.any((c) => c['id'] == _selectedCampaignId)) {
        _selectedCampaignId = null;
      }
      if (_selectedCampaignId == null) {
        _invoices = [];
        _selectedInvoiceId = null;
      } else {
        _invoices = await CampaignsApi.invoices(_selectedCampaignId!);
        if (_selectedInvoiceId != null &&
            !_invoices.any((i) => i['id'] == _selectedInvoiceId)) {
          _selectedInvoiceId = null;
        }
      }
      _items = await InvoiceExtractionsApi.listItems(
        campaignId: _selectedCampaignId,
        invoiceId: _selectedInvoiceId,
        status: null,
        lowReliabilityOnly: false,
        lowReliabilityThreshold: AppThresholds.lowReliabilityOnlyThreshold,
      );
    } catch (err) {
      _error = ApiClient.formatError(err);
    }
    if (mounted) {
      setState(() => _loading = false);
    }
  }

  Future<void> _extractForCampaign() async {
    final campaignId = _selectedCampaignId;
    if (campaignId == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Selectionne une campagne')),
      );
      return;
    }
    try {
      final invoices = await CampaignsApi.invoices(campaignId);
      if (invoices.isEmpty) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Aucune facture a traiter')),
        );
        return;
      }
      int extracted = 0;
      for (final invoice in invoices) {
        final id = invoice['id'] as int;
        final items = await InvoiceExtractionsApi.extractInvoice(id);
        extracted += items.length;
      }
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Extraction terminee ($extracted lignes)')),
      );
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  Future<void> _confirmSure() async {
    try {
      final updated = await InvoiceExtractionsApi.confirmSure(
        campaignId: _selectedCampaignId,
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Lignes confirmees: $updated')),
      );
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  Future<void> _addManualItem() async {
    if (_selectedInvoiceId == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Selectionne une facture')),
      );
      return;
    }
    final nameController = TextEditingController();
    final descController = TextEditingController();
    final qtyController = TextEditingController(text: '1');
    final unitController = TextEditingController();
    final priceController = TextEditingController();
    final currencyController = TextEditingController(text: 'DZD');

    final result = await showDialog<bool>(
      context: context,
      builder: (context) {
        final screen = MediaQuery.sizeOf(context);
        final dialogWidth =
            (screen.width * 0.92).clamp(300.0, 520.0).toDouble();
        return AlertDialog(
          title: const Text('Ajouter produit'),
          content: SizedBox(
            width: dialogWidth,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: nameController,
                    decoration: const InputDecoration(labelText: 'Nom produit'),
                  ),
                  TextField(
                    controller: descController,
                    decoration: const InputDecoration(labelText: 'Description'),
                  ),
                  TextField(
                    controller: qtyController,
                    decoration: const InputDecoration(labelText: 'Quantite'),
                    keyboardType: TextInputType.number,
                  ),
                  TextField(
                    controller: unitController,
                    decoration: const InputDecoration(labelText: 'Unite'),
                  ),
                  TextField(
                    controller: priceController,
                    decoration:
                        const InputDecoration(labelText: 'Prix unitaire'),
                    keyboardType: TextInputType.number,
                  ),
                  TextField(
                    controller: currencyController,
                    decoration: const InputDecoration(labelText: 'Devise'),
                  ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('Annuler')),
            ElevatedButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('Ajouter')),
          ],
        );
      },
    );

    if (result != true) return;

    final payload = <String, dynamic>{
      'invoice_id': _selectedInvoiceId,
      'product_name': nameController.text.trim(),
      'description': descController.text.trim().isEmpty
          ? null
          : descController.text.trim(),
      'quantity': double.tryParse(qtyController.text.trim()),
      'unit': unitController.text.trim().isEmpty
          ? null
          : unitController.text.trim(),
      'unit_price': double.tryParse(priceController.text.trim()),
      'currency': currencyController.text.trim().isEmpty
          ? null
          : currencyController.text.trim(),
      'status': 'CONFIRMED',
    };

    if ((payload['product_name'] as String).isEmpty) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Nom produit obligatoire')));
      return;
    }

    try {
      await InvoiceExtractionsApi.createItem(payload);
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Produit ajoute')));
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  Future<void> _openSelectedInvoice() async {
    final invoiceId = _selectedInvoiceId;
    if (invoiceId == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Selectionne une facture')),
      );
      return;
    }
    final invoice = _invoices.firstWhere(
      (i) => i['id'] == invoiceId,
      orElse: () => null,
    );
    if (invoice == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Facture introuvable')),
      );
      return;
    }
    final path = (invoice['file_path'] ?? '').toString().trim();
    final fileName = (invoice['file_name'] ?? '').toString().trim();
    await _openInvoiceFile(
      invoiceId: invoiceId,
      localPath: path,
      preferredName: fileName,
    );
  }

  Future<void> _openInvoiceById(int invoiceId) async {
    final invoice = _invoices.firstWhere(
      (i) => i['id'] == invoiceId,
      orElse: () => null,
    );
    if (invoice == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Facture introuvable')),
      );
      return;
    }
    final path = (invoice['file_path'] ?? '').toString().trim();
    final fileName = (invoice['file_name'] ?? '').toString().trim();
    await _openInvoiceFile(
      invoiceId: invoiceId,
      localPath: path,
      preferredName: fileName,
    );
  }

  Future<void> _openFilePath(String path) async {
    final opened = await _openWithShell(path);
    if (!opened) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Impossible d\'ouvrir le fichier')));
    }
  }

  Future<void> _openTechSheetByItem(dynamic item) async {
    final path = (item['tech_sheet_file_path'] ?? '').toString().trim();
    final fileName = (item['tech_sheet_file'] ?? '').toString().trim();
    final techIdRaw = item['tech_sheet_id'];
    final techId = techIdRaw is num ? techIdRaw.toInt() : null;
    if (path.isEmpty && techId == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Aucune fiche technique liee')),
      );
      return;
    }
    if (path.isNotEmpty &&
        (_isLocalWindowsPath(path) ||
            path.startsWith('http://') ||
            path.startsWith('https://'))) {
      final opened = await _openWithShell(path);
      if (opened) {
        return;
      }
    }
    if (techId == null) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Fiche technique introuvable')),
      );
      return;
    }
    try {
      final bytes = await CampaignsApi.downloadTechSheetFile(techId);
      await _openTempPdf(
        bytes: bytes,
        preferredName: fileName,
        fallbackBase: 'tech_sheet_$techId',
      );
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  Future<void> _updateItem(dynamic item) async {
    final nameController = TextEditingController(
      text: item['product_name']?.toString() ?? '',
    );
    final qtyController = TextEditingController(
      text: (item['corrected_quantity'] ?? item['quantity'] ?? '').toString(),
    );
    final unitController = TextEditingController(
      text:
          item['corrected_unit']?.toString() ?? item['unit']?.toString() ?? '',
    );
    final priceController = TextEditingController(
      text:
          (item['corrected_unit_price'] ?? item['unit_price'] ?? '').toString(),
    );
    final currencyController = TextEditingController(
      text: item['corrected_currency']?.toString() ??
          item['currency']?.toString() ??
          'DZD',
    );
    final notesController =
        TextEditingController(text: item['notes']?.toString() ?? '');

    final result = await showDialog<bool>(
      context: context,
      builder: (context) {
        final screen = MediaQuery.sizeOf(context);
        final dialogWidth =
            (screen.width * 0.92).clamp(300.0, 520.0).toDouble();
        return AlertDialog(
          title: const Text('Corriger la ligne'),
          content: SizedBox(
            width: dialogWidth,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: nameController,
                    decoration: const InputDecoration(
                        labelText: 'Nom extrait (facture)'),
                  ),
                  TextField(
                    controller: qtyController,
                    decoration: const InputDecoration(labelText: 'Quantite'),
                    keyboardType: TextInputType.number,
                  ),
                  TextField(
                    controller: unitController,
                    decoration: const InputDecoration(labelText: 'Unite'),
                  ),
                  TextField(
                    controller: priceController,
                    decoration:
                        const InputDecoration(labelText: 'Prix unitaire'),
                    keyboardType: TextInputType.number,
                  ),
                  TextField(
                    controller: currencyController,
                    decoration: const InputDecoration(labelText: 'Devise'),
                  ),
                  TextField(
                    controller: notesController,
                    decoration: const InputDecoration(labelText: 'Notes'),
                  ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('Annuler')),
            ElevatedButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('Recalculer match')),
          ],
        );
      },
    );

    if (result != true) {
      return;
    }

    final payload = <String, dynamic>{
      'product_name': nameController.text.trim(),
      // Clear previous matched target to force rematch from edited extracted name.
      'corrected_product_name': null,
      'corrected_unit': unitController.text.trim().isEmpty
          ? null
          : unitController.text.trim(),
      'corrected_quantity': double.tryParse(qtyController.text.trim()),
      'corrected_unit_price': double.tryParse(priceController.text.trim()),
      'corrected_currency': currencyController.text.trim().isEmpty
          ? null
          : currencyController.text.trim(),
      'notes': notesController.text.trim().isEmpty
          ? null
          : notesController.text.trim(),
      'status': 'TO_VERIFY',
    };

    try {
      await InvoiceExtractionsApi.updateItem(item['id'] as int, payload);
      await InvoiceExtractionsApi.rematchItem(item['id'] as int);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('Nom extrait modifie, match recalcule')));
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  Future<void> _confirmItem(dynamic item) async {
    try {
      await InvoiceExtractionsApi.updateItem(
        item['id'] as int,
        {'status': 'CONFIRMED'},
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Ligne confirmee')));
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  Future<void> _rejectItem(dynamic item) async {
    try {
      final currentNotes = (item['notes'] ?? '').toString();
      final notes = currentNotes.contains('rejected')
          ? currentNotes
          : (currentNotes.isEmpty ? 'rejected' : '$currentNotes | rejected');
      await InvoiceExtractionsApi.updateItem(
        item['id'] as int,
        {
          'status': 'TO_VERIFY',
          'notes': notes,
          'corrected_product_name': null,
          'corrected_unit': null,
          'corrected_quantity': null,
          'corrected_unit_price': null,
          'corrected_currency': null,
        },
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Ligne rejetee')));
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  String? _matchFromNotes(String? notes) {
    if (notes == null) return null;
    final emb = RegExp(r'emb_match=(.+?)(?:\s+emb_score=|$)').firstMatch(notes);
    if (emb != null) return emb.group(1)?.trim();
    final match = RegExp(r'match=(.+?)(?:\s+score=|$)').firstMatch(notes);
    return match?.group(1)?.trim();
  }

  String _invoiceFileName(int invoiceId) {
    for (final inv in _invoices) {
      if (inv['id'] == invoiceId) {
        final path = (inv['file_path'] ?? inv['file_name'] ?? '').toString();
        if (path.isEmpty) break;
        return path.split('\\').last;
      }
    }
    return 'Facture #$invoiceId';
  }

  double _invoiceReliability(List<dynamic> invoiceItems) {
    if (invoiceItems.isEmpty) return 1.0;
    final allConfirmed =
        invoiceItems.every((item) => item['status'] == 'CONFIRMED');
    if (allConfirmed) return 1.0;
    final unknown = invoiceItems.where((item) {
      final name = (item['product_name'] ?? '').toString().trim().toUpperCase();
      return name == 'UNKNOWN';
    }).length;
    return 1 - (unknown / invoiceItems.length);
  }

  String? _asPercent(dynamic raw) {
    if (raw == null) return null;
    final value = raw is num ? raw.toDouble() : double.tryParse(raw.toString());
    if (value == null) return null;
    final pct = value <= 1.0 ? (value * 100.0) : value;
    return '${pct.toStringAsFixed(1)}%';
  }

  bool _isFallbackApplied(dynamic item) {
    final notes = (item['notes'] ?? '').toString();
    return notes.contains('fallback_applied=1');
  }

  String? _fallbackReasonsFromNotes(String? notes) {
    if (notes == null || notes.isEmpty) return null;
    final match = RegExp(r'fallback_reasons=([^\s]+)').firstMatch(notes);
    final raw = match?.group(1)?.trim();
    if (raw == null || raw.isEmpty) return null;
    final labels = raw
        .split(',')
        .map((e) => e.trim())
        .where((e) => e.isNotEmpty)
        .map((e) {
          switch (e) {
            case 'low_gpt_match':
              return 'score GPT faible';
            case 'admin_name_noise':
              return 'nom parasite';
            case 'duplicate_name':
              return 'doublon nom';
            case 'amount_inconsistency':
              return 'montant incoherent';
            default:
              return e;
          }
        })
        .toList();
    if (labels.isEmpty) return null;
    return labels.join(', ');
  }

  Color _statusColor(String status, BuildContext context) {
    switch (status) {
      case 'CONFIRMED':
        return Colors.green.shade700;
      case 'TO_VERIFY':
        return Colors.orange.shade700;
      case 'PENDING':
        return Colors.grey.shade700;
      default:
        return Theme.of(context).colorScheme.primary;
    }
  }

  String _statusLabel(String status) {
    switch (status) {
      case 'CONFIRMED':
        return 'Confirme';
      case 'TO_VERIFY':
        return 'A verifier';
      case 'PENDING':
        return 'En attente';
      default:
        return status;
    }
  }

  Color _techSimilarityColor(String status, BuildContext context) {
    switch (status) {
      case 'conforme':
        return Colors.green.shade700;
      case 'partiel':
        return Colors.orange.shade700;
      case 'faible':
        return Colors.red.shade700;
      case 'no_tech_sheet':
        return Colors.red.shade700;
      case 'no_requested_reference':
        return Colors.deepOrange.shade700;
      default:
        return Theme.of(context).colorScheme.primary;
    }
  }

  String _techSimilarityLabel(String status) {
    switch (status) {
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
      case 'not_available':
        return 'N/A';
      default:
        return status.isEmpty ? 'N/A' : status;
    }
  }

  Future<void> _showTechSimilarityDetails(dynamic item) async {
    final breakdown = item['tech_sheet_similarity_breakdown'];
    if (breakdown is! Map) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Aucun detail de conformite fiche')),
      );
      return;
    }
    final componentsRaw = breakdown['components'];
    final components = componentsRaw is Map ? componentsRaw : const {};
    final requestedProduct =
        (breakdown['requested_product_name'] ?? '').toString().trim();
    final score = _asPercent(item['tech_sheet_similarity_display_score'] ??
            item['tech_sheet_similarity_score']) ??
        '-';
    final status = _techSimilarityLabel(
      (item['tech_sheet_similarity_display_status'] ??
              item['tech_sheet_similarity_status'] ??
              '')
          .toString(),
    );
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (context) {
        Widget componentRow(String key, String title) {
          final raw = components[key];
          final map = raw is Map ? raw : const {};
          final scoreRaw = map['score'];
          final scoreText = _asPercent(scoreRaw) ?? '-';
          final matched = map['matched'];
          final total = map['total'];
          String ratio = '-';
          if (matched is num && total is num && total.toInt() > 0) {
            ratio = '${matched.toInt()} / ${total.toInt()}';
          }
          return Padding(
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Row(
              children: [
                Expanded(child: Text(title)),
                Text(ratio,
                    style: const TextStyle(fontWeight: FontWeight.w600)),
                const SizedBox(width: 12),
                Text(scoreText),
              ],
            ),
          );
        }

        return AlertDialog(
          title: const Text('Conformite fiche technique'),
          content: SizedBox(
            width: 560,
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Score global: $score'),
                  Text('Statut: $status'),
                  if (requestedProduct.isNotEmpty)
                    Text('Produit demande: $requestedProduct'),
                  const SizedBox(height: 10),
                  const Divider(height: 1),
                  const SizedBox(height: 8),
                  componentRow('characteristics', 'Caracteristiques'),
                  componentRow('requirements', 'Exigences'),
                  componentRow('accessories', 'Accessoires'),
                  componentRow('description', 'Description'),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Fermer'),
            ),
          ],
        );
      },
    );
  }

  Widget _summaryChip(
    BuildContext context, {
    required IconData icon,
    required String label,
    required String value,
    Color? color,
  }) {
    final baseColor = color ?? Theme.of(context).colorScheme.primary;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: baseColor.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(
          color: baseColor.withValues(alpha: 0.25),
        ),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 16, color: baseColor),
          const SizedBox(width: 6),
          Text(
            '$label: $value',
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: baseColor,
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildInvoiceGroupedList() {
    final grouped = <int, List<dynamic>>{};
    for (final item in _items) {
      final invoiceId = (item['invoice_id'] as num?)?.toInt();
      if (invoiceId == null) continue;
      grouped.putIfAbsent(invoiceId, () => <dynamic>[]).add(item);
    }
    bool isVisibleByFallback(dynamic item) {
      final raw = (item['product_name'] ?? '').toString().trim();
      if (raw.isEmpty || raw.toUpperCase() == 'UNKNOWN') return false;
      return _isFallbackApplied(item);
    }

    final entries = grouped.entries
        .where((entry) => entry.value.any(isVisibleByFallback))
        .toList()
      ..sort((a, b) => a.key.compareTo(b.key));

    return ListView.separated(
      itemCount: entries.length,
      separatorBuilder: (_, __) => const SizedBox(height: 12),
      itemBuilder: (context, index) {
        final entry = entries[index];
        final invoiceId = entry.key;
        final invoiceItems = entry.value;
        final visibleItems = invoiceItems.where(isVisibleByFallback).toList();
        final reliability = _invoiceReliability(invoiceItems);
        final reliabilityPct = (reliability * 100.0).toStringAsFixed(1);
        final isLow = reliability <= AppThresholds.lowReliabilityOnlyThreshold;
        final invoiceName = _invoiceFileName(invoiceId);

        return Card(
          elevation: 1,
          child: ExpansionTile(
            initiallyExpanded: true,
            tilePadding:
                const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
            title: Text(
              invoiceName,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14),
            ),
            subtitle: Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Wrap(
                spacing: 8,
                runSpacing: 6,
                children: [
                  _summaryChip(
                    context,
                    icon: Icons.analytics_outlined,
                    label: 'Fiabilite',
                    value: '$reliabilityPct%',
                    color: isLow ? Colors.red.shade700 : Colors.green.shade700,
                  ),
                  _summaryChip(
                    context,
                    icon: Icons.view_list_outlined,
                    label: 'Produits affiches',
                    value: visibleItems.length.toString(),
                  ),
                  _summaryChip(
                    context,
                    icon: Icons.receipt_long_outlined,
                    label: 'Total extrait',
                    value: invoiceItems.length.toString(),
                  ),
                ],
              ),
            ),
            trailing: IconButton(
              tooltip: 'Ouvrir facture PDF',
              onPressed: () => _openInvoiceById(invoiceId),
              icon: const Icon(Icons.picture_as_pdf),
            ),
            childrenPadding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
            children: [
              if (visibleItems.isEmpty)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                    'Aucun produit fallback pour cette facture.',
                    style: TextStyle(color: Colors.grey.shade700),
                  ),
                ),
              ...visibleItems.map((item) {
                final name = item['product_name']?.toString() ?? '';
                final description = item['description']?.toString() ?? '';
                final supplier = item['supplier_name']?.toString() ?? '';
                final qty = item['quantity']?.toString() ?? '-';
                final price = item['unit_price']?.toString() ?? '-';
                final unit = item['unit']?.toString() ?? '';
                final notes = item['notes']?.toString();
                final fallbackReasons = _fallbackReasonsFromNotes(notes);
                final globalConformity = _asPercent(
                  item['tech_sheet_similarity_display_score'] ??
                      item['tech_sheet_similarity_score'],
                );
                final matchedProductName =
                    (item['matched_product_name'] ?? '').toString().trim();
                final matched = matchedProductName.isNotEmpty
                    ? matchedProductName
                    : _matchFromNotes(notes);
                final matchedCharsRaw = item['matched_characteristics'];
                final totalCharsRaw = item['total_characteristics'];
                final matchedChars =
                    matchedCharsRaw is num ? matchedCharsRaw.toInt() : null;
                final totalChars =
                    totalCharsRaw is num ? totalCharsRaw.toInt() : null;
                final techSimScore = _asPercent(
                    item['tech_sheet_similarity_display_score'] ??
                        item['tech_sheet_similarity_score']);
                final techSimStatus =
                    (item['tech_sheet_similarity_display_status'] ??
                            item['tech_sheet_similarity_status'] ??
                            '')
                        .toString();
                final status = item['status']?.toString() ?? '';
                final isConfirmed = status == 'CONFIRMED';

                return Container(
                  margin: const EdgeInsets.only(bottom: 8),
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Colors.grey.shade50,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: Colors.grey.shade200),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        name.isEmpty ? 'Produit sans nom' : name,
                        style: const TextStyle(
                            fontWeight: FontWeight.w700, fontSize: 13),
                      ),
                      const SizedBox(height: 4),
                      if (supplier.isNotEmpty)
                        Text('Fournisseur: $supplier',
                            style: const TextStyle(fontSize: 12)),
                      if (description.isNotEmpty)
                        Text(
                          'Description: $description',
                          style: const TextStyle(fontSize: 12),
                        ),
                      const SizedBox(height: 6),
                      Wrap(
                        spacing: 8,
                        runSpacing: 6,
                        children: [
                          _summaryChip(
                            context,
                            icon: Icons.flag_outlined,
                            label: 'Statut',
                            value: _statusLabel(status),
                            color: _statusColor(status, context),
                          ),
                          if (fallbackReasons != null &&
                              fallbackReasons.isNotEmpty)
                            _summaryChip(
                              context,
                              icon: Icons.auto_fix_high_outlined,
                              label: 'Fallback',
                              value: fallbackReasons,
                              color: Colors.deepOrange.shade700,
                            ),
                          _summaryChip(
                            context,
                            icon: Icons.inventory_2_outlined,
                            label: 'Qty',
                            value: '$qty $unit',
                          ),
                          _summaryChip(
                            context,
                            icon: Icons.attach_money_outlined,
                            label: 'Prix',
                            value: '$price ${item['currency'] ?? ''}',
                          ),
                          if (!_simpleView)
                            _summaryChip(
                              context,
                              icon: Icons.auto_graph_outlined,
                              label: 'Conformite globale',
                              value: globalConformity ?? '-',
                            ),
                          if (!_simpleView &&
                              matchedChars != null &&
                              totalChars != null &&
                              totalChars > 0)
                            _summaryChip(
                              context,
                              icon: Icons.rule_folder_outlined,
                              label: 'Caracteristiques',
                              value: '$matchedChars / $totalChars',
                            ),
                          if (techSimStatus.isNotEmpty &&
                              techSimStatus != 'not_available')
                            _summaryChip(
                              context,
                              icon: Icons.linked_camera_outlined,
                              label: 'Conformite fiche',
                              value: techSimScore ?? '-',
                              color:
                                  _techSimilarityColor(techSimStatus, context),
                            ),
                          if (techSimStatus.isNotEmpty &&
                              techSimStatus != 'not_available')
                            _summaryChip(
                              context,
                              icon: Icons.verified_outlined,
                              label: 'Statut fiche',
                              value: _techSimilarityLabel(techSimStatus),
                              color:
                                  _techSimilarityColor(techSimStatus, context),
                            ),
                        ],
                      ),
                      if (matched != null && matched.trim().isNotEmpty) ...[
                        const SizedBox(height: 6),
                        Container(
                          width: double.infinity,
                          padding: const EdgeInsets.symmetric(
                            horizontal: 10,
                            vertical: 8,
                          ),
                          decoration: BoxDecoration(
                            color: Colors.indigo.withValues(alpha: 0.08),
                            borderRadius: BorderRadius.circular(8),
                            border: Border.all(
                              color: Colors.indigo.withValues(alpha: 0.22),
                            ),
                          ),
                          child: Text(
                            matched.trim(),
                            style: const TextStyle(
                              fontSize: 15,
                              fontWeight: FontWeight.w800,
                              color: Colors.indigo,
                            ),
                          ),
                        ),
                      ],
                      const SizedBox(height: 8),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          if ((item['tech_sheet_file_path'] ?? '')
                              .toString()
                              .trim()
                              .isNotEmpty)
                            OutlinedButton.icon(
                              onPressed: () => _openTechSheetByItem(item),
                              icon: const Icon(Icons.description_outlined,
                                  size: 16),
                              label: const Text('Fiche technique'),
                            ),
                          if (item['tech_sheet_similarity_breakdown'] is Map)
                            OutlinedButton.icon(
                              onPressed: () => _showTechSimilarityDetails(item),
                              icon:
                                  const Icon(Icons.insights_outlined, size: 16),
                              label: const Text('Score fiche'),
                            ),
                          OutlinedButton.icon(
                            onPressed: () => _rejectItem(item),
                            icon: const Icon(Icons.close, size: 16),
                            label: const Text('Rejeter'),
                          ),
                          OutlinedButton.icon(
                            onPressed:
                                isConfirmed ? null : () => _confirmItem(item),
                            icon: const Icon(Icons.check, size: 16),
                            label: const Text('Valider'),
                          ),
                          ElevatedButton.icon(
                            onPressed: () => _updateItem(item),
                            icon: const Icon(Icons.edit, size: 16),
                            label: const Text('Corriger'),
                          ),
                        ],
                      ),
                    ],
                  ),
                );
              }),
            ],
          ),
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return Center(child: Text('Error: $_error'));
    }

    final invoiceCount = _items
        .map((i) => (i['invoice_id'] as num?)?.toInt())
        .whereType<int>()
        .toSet()
        .length;
    final toVerifyCount = _items
        .where((i) => (i['status']?.toString() ?? '') == 'TO_VERIFY')
        .length;
    final confirmedCount = _items
        .where((i) => (i['status']?.toString() ?? '') == 'CONFIRMED')
        .length;
    final screenWidth = MediaQuery.sizeOf(context).width;
    final filterPanelWidth =
        screenWidth < 900 ? 250.0 : (screenWidth < 1200 ? 300.0 : 340.0);

    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'A verifier',
            style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _summaryChip(
                context,
                icon: Icons.receipt_long_outlined,
                label: 'Factures',
                value: invoiceCount.toString(),
              ),
              _summaryChip(
                context,
                icon: Icons.pending_actions_outlined,
                label: 'A verifier',
                value: toVerifyCount.toString(),
                color: Colors.orange.shade700,
              ),
              _summaryChip(
                context,
                icon: Icons.check_circle_outline,
                label: 'Confirmes',
                value: confirmedCount.toString(),
                color: Colors.green.shade700,
              ),
              _summaryChip(
                context,
                icon: _simpleView
                    ? Icons.visibility_outlined
                    : Icons.tune_outlined,
                label: 'Vue',
                value: _simpleView ? 'Simplifiee' : 'Detaillee',
              ),
            ],
          ),
          const SizedBox(height: 12),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Wrap(
                spacing: 12,
                runSpacing: 10,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  const Text('Filtres',
                      style: TextStyle(fontWeight: FontWeight.bold)),
                  DropdownButton<int?>(
                    value: _selectedCampaignId,
                    hint: const Text('Toutes campagnes'),
                    items: [
                      const DropdownMenuItem<int?>(
                          value: null, child: Text('Toutes campagnes')),
                      ..._campaigns.map(
                        (c) => DropdownMenuItem<int?>(
                          value: c['id'] as int,
                          child: Text(c['title'] as String),
                        ),
                      ),
                    ],
                    onChanged: (value) async {
                      setState(() => _selectedCampaignId = value);
                      await _load();
                    },
                  ),
                  DropdownButton<int?>(
                    value: _selectedInvoiceId,
                    hint: const Text('Toutes factures'),
                    items: [
                      const DropdownMenuItem<int?>(
                          value: null, child: Text('Toutes factures')),
                      ..._invoices.map(
                        (i) => DropdownMenuItem<int?>(
                          value: i['id'] as int,
                          child:
                              Text((i['file_path'] as String).split('\\').last),
                        ),
                      ),
                    ],
                    onChanged: _selectedCampaignId == null
                        ? null
                        : (value) async {
                            setState(() => _selectedInvoiceId = value);
                            await _load();
                          },
                  ),
                  const Text('Statuts: tous'),
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Text('Vue simplifiee'),
                      Switch(
                        value: _simpleView,
                        onChanged: (value) {
                          setState(() => _simpleView = value);
                        },
                      ),
                    ],
                  ),
                  SizedBox(
                    width: filterPanelWidth,
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Filtre fixe: factures avec fallback (produits fallback uniquement)',
                          style: const TextStyle(fontSize: 12),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 8),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  const Text('Actions',
                      style: TextStyle(fontWeight: FontWeight.bold)),
                  OutlinedButton.icon(
                    onPressed: _load,
                    icon: const Icon(Icons.refresh, size: 16),
                    label: const Text('Rafraichir'),
                  ),
                  OutlinedButton.icon(
                    onPressed: _extractForCampaign,
                    icon: const Icon(Icons.auto_fix_high, size: 16),
                    label: const Text('Extraire PDFs'),
                  ),
                  OutlinedButton.icon(
                    onPressed: _confirmSure,
                    icon: const Icon(Icons.done_all, size: 16),
                    label: const Text('Valider sur'),
                  ),
                  OutlinedButton.icon(
                    onPressed: _selectedInvoiceId == null
                        ? null
                        : _openSelectedInvoice,
                    icon: const Icon(Icons.picture_as_pdf, size: 16),
                    label: const Text('Voir facture'),
                  ),
                  OutlinedButton.icon(
                    onPressed:
                        _selectedInvoiceId == null ? null : _addManualItem,
                    icon: const Icon(Icons.add, size: 16),
                    label: const Text('Ajouter produit'),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),
          Expanded(
            child: _items.isEmpty
                ? const EmptyState(
                    icon: Icons.check_circle_outline,
                    title: 'Rien a verifier',
                    message: 'Toutes les lignes sont validees.',
                  )
                : _buildInvoiceGroupedList(),
          ),
        ],
      ),
    );
  }
}
