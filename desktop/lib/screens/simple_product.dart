import 'dart:async';
import 'package:flutter/material.dart';

import '../api/campaigns_api.dart';
import '../api/client.dart';
import '../utils/file_opening.dart';
import '../widgets/empty_state.dart';

class SimpleProductScreen extends StatefulWidget {
  const SimpleProductScreen({super.key});

  @override
  State<SimpleProductScreen> createState() => _SimpleProductScreenState();
}

class _SimpleProductScreenState extends State<SimpleProductScreen> {
  static const int _pageSize = 50;

  final TextEditingController _searchController = TextEditingController();

  bool _loadingCampaigns = true;
  bool _loadingRows = false;
  String? _error;
  List<dynamic> _campaigns = [];
  List<dynamic> _rows = [];
  int? _selectedCampaignId;
  int _rowsRequestId = 0;
  String _searchQuery = '';

  @override
  void initState() {
    super.initState();
    _searchController.addListener(() {
      if (!mounted) return;
      setState(() => _searchQuery = _searchController.text.trim().toLowerCase());
    });
    unawaited(_loadCampaigns());
  }

  @override
  void dispose() {
    _searchController.dispose();
    super.dispose();
  }

  String _campaignDisplayLabel(dynamic raw) {
    if (raw is! Map) return '-';
    final id = raw['id'] is num ? (raw['id'] as num).toInt() : 0;
    final title = (raw['title'] ?? '').toString().trim();
    final establishment = (raw['establishment_name'] ?? '').toString().trim();
    final baseTitle = title.isEmpty ? 'CAMP $id' : title;
    if (establishment.isEmpty) return baseTitle;
    return '$baseTitle - $establishment';
  }

  void _showError(Object err) {
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
  }

  Future<void> _loadCampaigns() async {
    setState(() {
      _loadingCampaigns = true;
      _error = null;
    });
    try {
      final campaigns = await CampaignsApi.list();
      campaigns.sort((a, b) {
        final aId = a is Map && a['id'] is num ? (a['id'] as num).toInt() : -1;
        final bId = b is Map && b['id'] is num ? (b['id'] as num).toInt() : -1;
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
      if (!mounted) return;
      setState(() {
        _campaigns = campaigns;
        _selectedCampaignId = selected;
        _loadingCampaigns = false;
      });
      if (selected != null) {
        await _loadRows();
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
      setState(() => _rows = []);
      return;
    }
    final requestId = ++_rowsRequestId;
    setState(() {
      _loadingRows = true;
      _error = null;
    });
    try {
      final firstPayload = await CampaignsApi.simpleProductOffers(
        campaignId,
        page: 1,
        pageSize: _pageSize,
      );
      if (!mounted || requestId != _rowsRequestId) return;
      final allRows =
          List<dynamic>.from((firstPayload['items'] as List?) ?? const []);
      var page = 2;
      var hasMore = firstPayload['has_more'] == true;
      setState(() => _rows = List<dynamic>.from(allRows));
      while (hasMore && page <= 200) {
        final payload = await CampaignsApi.simpleProductOffers(
          campaignId,
          page: page,
          pageSize: _pageSize,
        );
        if (!mounted || requestId != _rowsRequestId) return;
        allRows.addAll(List<dynamic>.from((payload['items'] as List?) ?? const []));
        hasMore = payload['has_more'] == true;
        page += 1;
        setState(() => _rows = List<dynamic>.from(allRows));
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

  Future<bool> _openWithShell(String path) async => openWithShell(path);

  Future<void> _openLocalFile(String? path) async {
    final value = (path ?? '').trim();
    if (value.isEmpty) return;
    final opened = await _openWithShell(value);
    if (!opened && mounted) {
      _showError(Exception('Impossible d\'ouvrir le fichier'));
    }
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
      prefix: 'simple_product',
    );
    await _openLocalFile(path);
  }

  bool _isLocalWindowsPath(String value) => isLocalWindowsPath(value);

  Future<void> _openInvoiceFile(Map<String, dynamic> offer) async {
    final invoicePath = (offer['invoice_file_path'] ?? '').toString().trim();
    final invoiceFile = (offer['invoice_file_name'] ?? '').toString().trim();
    final invoiceIdRaw = offer['invoice_id'];
    final invoiceId = invoiceIdRaw is num ? invoiceIdRaw.toInt() : null;

    if (invoicePath.isNotEmpty &&
        (_isLocalWindowsPath(invoicePath) ||
            invoicePath.startsWith('http://') ||
            invoicePath.startsWith('https://'))) {
      final opened = await _openWithShell(invoicePath);
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

  double? _toDouble(dynamic value) {
    if (value is num) return value.toDouble();
    if (value is String) return double.tryParse(value.trim().replaceAll(',', '.'));
    return null;
  }

  String _fmtMoney(dynamic value) {
    final number = _toDouble(value);
    if (number == null) return '-';
    final raw = number == number.roundToDouble()
        ? number.toInt().toString()
        : number.toStringAsFixed(2);
    return raw.replaceAll('.', ',');
  }

  String _fmtQty(dynamic value) {
    final number = _toDouble(value);
    if (number == null) return '-';
    return number == number.roundToDouble()
        ? number.toInt().toString()
        : number.toStringAsFixed(2).replaceAll('.', ',');
  }

  Color _scoreColor(double score) {
    if (score >= 0.85) return const Color(0xFF0F766E);
    if (score >= 0.70) return const Color(0xFF2563EB);
    if (score >= 0.50) return const Color(0xFFB45309);
    return const Color(0xFFB42318);
  }

  List<Map<String, dynamic>> _offersForRow(dynamic rowRaw) {
    if (rowRaw is! Map) return const [];
    final offersRaw = rowRaw['offers'];
    if (offersRaw is! List) return const [];
    final out = <Map<String, dynamic>>[];
    for (final raw in offersRaw) {
      if (raw is Map) out.add(Map<String, dynamic>.from(raw));
    }
    return out;
  }

  List<dynamic> get _filteredRows {
    if (_searchQuery.isEmpty) return _rows;
    return _rows.where((raw) {
      if (raw is! Map) return false;
      final productName = (raw['product_name'] ?? '').toString().toLowerCase();
      if (productName.contains(_searchQuery)) return true;
      for (final offer in _offersForRow(raw)) {
        final offered = (offer['produit_offert'] ?? '').toString().toLowerCase();
        final supplier = (offer['supplier_name'] ?? '').toString().toLowerCase();
        if (offered.contains(_searchQuery) || supplier.contains(_searchQuery)) {
          return true;
        }
      }
      return false;
    }).toList();
  }

  Widget _selectorBar() {
    return Wrap(
      spacing: 12,
      runSpacing: 12,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: [
        ConstrainedBox(
          constraints: const BoxConstraints(minWidth: 260, maxWidth: 460),
          child: DropdownButtonFormField<int>(
            value: _selectedCampaignId,
            decoration: const InputDecoration(
              labelText: 'Campaign',
              border: OutlineInputBorder(),
              isDense: true,
            ),
            items: _campaigns.map<DropdownMenuItem<int>>((raw) {
              if (raw is! Map) {
                return const DropdownMenuItem<int>(value: -1, child: Text('-'));
              }
              final id = raw['id'] is num ? (raw['id'] as num).toInt() : -1;
              return DropdownMenuItem<int>(
                value: id,
                child: Text(
                  _campaignDisplayLabel(raw),
                  overflow: TextOverflow.ellipsis,
                  maxLines: 1,
                ),
              );
            }).toList(),
            onChanged: (value) async {
              if (value == null || value == _selectedCampaignId) return;
              setState(() => _selectedCampaignId = value);
              await _loadRows();
            },
          ),
        ),
        SizedBox(
          width: 320,
          child: TextField(
            controller: _searchController,
            decoration: const InputDecoration(
              labelText: 'Recherche produit / fournisseur',
              border: OutlineInputBorder(),
              isDense: true,
              prefixIcon: Icon(Icons.search),
            ),
          ),
        ),
        FilledButton.icon(
          onPressed: _loadingRows ? null : _loadRows,
          icon: const Icon(Icons.refresh),
          label: const Text('Actualiser'),
        ),
      ],
    );
  }

  Widget _offerCard(Map<String, dynamic> offer) {
    final score = _toDouble(offer['score']) ?? 0.0;
    final conforme = offer['conforme'] == true;
    final differences = (offer['difference'] as List?)?.map((e) => e.toString()).where((e) => e.trim().isNotEmpty).toList() ?? const <String>[];
    final invoiceName = (offer['invoice_file_name'] ?? '').toString().trim();
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: const Color(0xFFD9E2EC)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  (offer['produit_offert'] ?? '-').toString(),
                  style: const TextStyle(
                    fontWeight: FontWeight.w700,
                    fontSize: 14,
                  ),
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                decoration: BoxDecoration(
                  color: _scoreColor(score).withOpacity(0.12),
                  borderRadius: BorderRadius.circular(999),
                ),
                child: Text(
                  'Score ${(score * 100).toStringAsFixed(0)}%',
                  style: TextStyle(
                    color: _scoreColor(score),
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                decoration: BoxDecoration(
                  color: (conforme ? const Color(0xFFE8F5E9) : const Color(0xFFFDECEC)),
                  borderRadius: BorderRadius.circular(999),
                ),
                child: Text(
                  conforme ? 'Conforme' : 'Non conforme',
                  style: TextStyle(
                    color: conforme ? const Color(0xFF1B4332) : const Color(0xFFB42318),
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            'Fournisseur: ${(offer['supplier_name'] ?? '-').toString()}',
            style: const TextStyle(fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _infoChip('PU facture', _fmtMoney(offer['prix_unitaire_facture'])),
              _infoChip('Prix équiv.', _fmtMoney(offer['prix_equivalent'])),
              _infoChip('TVA', _fmtMoney(offer['tva'])),
              _infoChip('Qté demandée', _fmtQty(offer['quantite_demandee'])),
              _infoChip('Qté offerte', _fmtQty(offer['quantite_offerte'])),
              _infoChip('Source', (offer['source_engine'] ?? '-').toString()),
            ],
          ),
          const SizedBox(height: 10),
          if (((offer['similarite'] ?? '').toString().trim()).isNotEmpty)
            Text('Similarité: ${(offer['similarite'] ?? '').toString().trim()}'),
          if (((offer['raison'] ?? '').toString().trim()).isNotEmpty) ...[
            const SizedBox(height: 6),
            Text('Raison: ${(offer['raison'] ?? '').toString().trim()}'),
          ],
          if (differences.isNotEmpty) ...[
            const SizedBox(height: 8),
            const Text(
              'Différences',
              style: TextStyle(fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 4),
            ...differences.take(6).map((item) => Text('• $item')),
          ],
          if (((offer['produit_offert_ocr_snippet'] ?? '').toString().trim()).isNotEmpty) ...[
            const SizedBox(height: 8),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: const Color(0xFFF8FAFC),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Text(
                (offer['produit_offert_ocr_snippet'] ?? '').toString(),
                style: const TextStyle(fontSize: 12.5, color: Color(0xFF334155)),
              ),
            ),
          ],
          if (invoiceName.isNotEmpty) ...[
            const SizedBox(height: 10),
            Align(
              alignment: Alignment.centerLeft,
              child: OutlinedButton.icon(
                onPressed: () => _openInvoiceFile(offer),
                icon: const Icon(Icons.picture_as_pdf_outlined),
                label: Text(invoiceName),
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _infoChip(String label, String value) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: const Color(0xFFF8FAFC),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Text(
        '$label: $value',
        style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w600),
      ),
    );
  }

  Widget _rowCard(dynamic raw) {
    if (raw is! Map) return const SizedBox.shrink();
    final row = Map<String, dynamic>.from(raw);
    final offers = _offersForRow(row);
    final requestedSnippet = (row['requested_ocr_snippet'] ?? '').toString().trim();
    return Container(
      margin: const EdgeInsets.only(bottom: 14),
      decoration: BoxDecoration(
        color: const Color(0xFFFDFDFD),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: const Color(0xFFD9E2EC)),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withOpacity(0.04),
            blurRadius: 10,
            offset: const Offset(0, 4),
          ),
        ],
      ),
      child: ExpansionTile(
        tilePadding: const EdgeInsets.symmetric(horizontal: 18, vertical: 10),
        childrenPadding: const EdgeInsets.fromLTRB(18, 0, 18, 18),
        title: Text(
          (row['product_name'] ?? '-').toString(),
          style: const TextStyle(fontWeight: FontWeight.w800),
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 6),
          child: Text(
            'Qté demandée: ${_fmtQty(row['requested_qty'])} | Offres retenues: ${offers.length}',
          ),
        ),
        children: [
          if (requestedSnippet.isNotEmpty) ...[
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: const Color(0xFFF8FAFC),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: const Color(0xFFE2E8F0)),
              ),
              child: Text(
                requestedSnippet,
                style: const TextStyle(fontSize: 12.5, color: Color(0xFF334155)),
              ),
            ),
            const SizedBox(height: 12),
          ],
          if (offers.isEmpty)
            const EmptyState(
              icon: Icons.playlist_remove,
              title: 'Aucune offre retenue',
              message: 'GPT n’a retenu aucune offre conforme pour ce produit.',
            )
          else
            Column(
              children: [
                for (final offer in offers) ...[
                  _offerCard(offer),
                  if (offer != offers.last) const SizedBox(height: 10),
                ],
              ],
            ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final filteredRows = _filteredRows;
    if (_loadingCampaigns) {
      return const Center(child: CircularProgressIndicator());
    }
    return Padding(
      padding: const EdgeInsets.all(18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Produit simple',
            style: Theme.of(context).textTheme.headlineSmall?.copyWith(
                  fontWeight: FontWeight.w800,
                ),
          ),
          const SizedBox(height: 6),
          Text(
            'Comparaison GPT 5.1 sur base OCR/JSON, top 2 offres par produit demandé.',
            style: Theme.of(context).textTheme.bodyMedium,
          ),
          const SizedBox(height: 16),
          _selectorBar(),
          const SizedBox(height: 16),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Text(
                _error!,
                style: const TextStyle(color: Color(0xFFB42318)),
              ),
            ),
          if (_loadingRows)
            const Expanded(child: Center(child: CircularProgressIndicator()))
          else if (_selectedCampaignId == null)
            const Expanded(
              child: EmptyState(
                icon: Icons.folder_off_outlined,
                title: 'Aucune campagne',
                message: 'Crée ou sélectionne une campagne pour voir Produit simple.',
              ),
            )
          else if (filteredRows.isEmpty)
            const Expanded(
              child: EmptyState(
                icon: Icons.search_off_outlined,
                title: 'Aucun résultat',
                message: 'Aucune ligne ne correspond à la recherche ou GPT n’a rien retenu.',
              ),
            )
          else
            Expanded(
              child: ListView.builder(
                itemCount: filteredRows.length,
                itemBuilder: (context, index) => _rowCard(filteredRows[index]),
              ),
            ),
        ],
      ),
    );
  }
}
