import 'package:flutter/material.dart';

import '../api/campaigns_api.dart';
import '../api/client.dart';

class ImportedFilesScreen extends StatefulWidget {
  const ImportedFilesScreen({super.key});

  @override
  State<ImportedFilesScreen> createState() => _ImportedFilesScreenState();
}

class _ImportedFilesScreenState extends State<ImportedFilesScreen> {
  final TextEditingController _searchController = TextEditingController();
  final Set<String> _selectedKeys = <String>{};

  List<dynamic> _files = <dynamic>[];
  List<dynamic> _campaigns = <dynamic>[];
  bool _loading = true;
  bool _saving = false;
  bool _extracting = false;
  bool _includeTechSheets = true;
  int? _targetCampaignId;
  int? _campaignFilterId;
  String _search = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _searchController.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() => _loading = true);
    try {
      final campaigns = await CampaignsApi.list();
      final files =
          await CampaignsApi.importedFiles(includeTechSheets: _includeTechSheets);
      if (!mounted) return;
      int? targetCampaignId = _targetCampaignId;
      if (targetCampaignId == null && campaigns.isNotEmpty) {
        targetCampaignId = (campaigns.first as Map)['id'] as int?;
      }
      setState(() {
        _campaigns = campaigns;
        _files = files;
        _targetCampaignId = targetCampaignId;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() => _loading = false);
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    }
  }

  String _rowKey(dynamic row) {
    final map = Map<String, dynamic>.from(row as Map);
    return '${map['file_type']}:${map['file_id']}';
  }

  List<dynamic> get _filteredFiles {
    final query = _search.trim().toLowerCase();
    final filtered = _files.where((raw) {
      final row = Map<String, dynamic>.from(raw as Map);
      if (_campaignFilterId != null && row['campaign_id'] != _campaignFilterId) {
        return false;
      }
      if (query.isEmpty) {
        return true;
      }
      final haystack = [
        row['file_name'],
        row['sender_email'],
        row['sender_name'],
        row['subject'],
        row['campaign_title'],
        row['processing_mode'],
      ].whereType<String>().join(' ').toLowerCase();
      return haystack.contains(query);
    }).toList();
    filtered.sort((a, b) {
      final left = Map<String, dynamic>.from(a as Map);
      final right = Map<String, dynamic>.from(b as Map);
      final leftCampaign = (left['campaign_title'] as String? ?? '').trim();
      final rightCampaign = (right['campaign_title'] as String? ?? '').trim();
      final byCampaign = leftCampaign.compareTo(rightCampaign);
      if (byCampaign != 0) return byCampaign;
      return 0;
    });
    return filtered;
  }

  Future<void> _applyReassign() async {
    final campaignId = _targetCampaignId;
    if (campaignId == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Choisis une campagne cible')),
      );
      return;
    }
    final selectedRows = _files.where((raw) {
      return _selectedKeys.contains(_rowKey(raw));
    }).toList();
    if (selectedRows.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Aucun fichier selectionne')),
      );
      return;
    }

    final invoiceIds = <int>[];
    final techSheetIds = <int>[];
    for (final raw in selectedRows) {
      final row = Map<String, dynamic>.from(raw as Map);
      final id = row['file_id'] as int?;
      if (id == null) continue;
      final fileType = (row['file_type'] as String? ?? '').trim().toLowerCase();
      if (fileType == 'invoice') {
        invoiceIds.add(id);
      } else if (fileType == 'tech_sheet') {
        techSheetIds.add(id);
      }
    }

    setState(() => _saving = true);
    try {
      final result = await CampaignsApi.reassignImportedFiles(
        campaignId: campaignId,
        invoiceIds: invoiceIds,
        techSheetIds: techSheetIds,
      );
      if (!mounted) return;
      _selectedKeys.clear();
      final movedInvoices = result['moved_invoices'] ?? 0;
      final movedTechSheets = result['moved_tech_sheets'] ?? 0;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            'Reaffectation terminee: $movedInvoices facture(s), $movedTechSheets fiche(s) technique(s)',
          ),
        ),
      );
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    } finally {
      if (mounted) {
        setState(() => _saving = false);
      }
    }
  }

  Future<void> _extractSelection() async {
    final selectedRows = _files.where((raw) {
      return _selectedKeys.contains(_rowKey(raw));
    }).toList();
    if (selectedRows.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Aucun fichier selectionne')),
      );
      return;
    }

    final invoiceIds = <int>[];
    final techSheetIds = <int>[];
    for (final raw in selectedRows) {
      final row = Map<String, dynamic>.from(raw as Map);
      final id = row['file_id'] as int?;
      if (id == null) continue;
      final fileType = (row['file_type'] as String? ?? '').trim().toLowerCase();
      if (fileType == 'invoice') {
        invoiceIds.add(id);
      } else if (fileType == 'tech_sheet') {
        techSheetIds.add(id);
      }
    }

    setState(() => _extracting = true);
    try {
      final result = await CampaignsApi.extractImportedFiles(
        invoiceIds: invoiceIds,
        techSheetIds: techSheetIds,
        force: true,
      );
      if (!mounted) return;
      final extractedInvoices = result['extracted_invoices'] ?? 0;
      final extractedItems = result['extracted_invoice_items'] ?? 0;
      final extractedTechSheets = result['extracted_tech_sheets'] ?? 0;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            'Extraction terminee: $extractedInvoices fichier(s) facture, $extractedItems ligne(s), $extractedTechSheets fiche(s) technique(s)',
          ),
        ),
      );
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
    } finally {
      if (mounted) {
        setState(() => _extracting = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final filtered = _filteredFiles;
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Text(
                  'Fichiers importes',
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
                const SizedBox(width: 12),
                Text(
                  '${filtered.length} fichier(s)',
                  style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                        color: scheme.onSurfaceVariant,
                      ),
                ),
                const Spacer(),
                IconButton(
                  tooltip: 'Rafraichir',
                  onPressed: _loading ? null : _load,
                  icon: const Icon(Icons.refresh),
                ),
              ],
            ),
            const SizedBox(height: 16),
            Wrap(
              spacing: 12,
              runSpacing: 12,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                SizedBox(
                  width: 320,
                  child: TextField(
                    controller: _searchController,
                    decoration: const InputDecoration(
                      labelText: 'Recherche',
                      prefixIcon: Icon(Icons.search),
                    ),
                    onChanged: (value) => setState(() => _search = value),
                  ),
                ),
                SizedBox(
                  width: 280,
                  child: DropdownButtonFormField<int?>(
                    value: _campaignFilterId,
                    decoration: const InputDecoration(
                      labelText: 'Afficher campagne',
                    ),
                    items: [
                      const DropdownMenuItem<int?>(
                        value: null,
                        child: Text('Toutes les campagnes'),
                      ),
                      ..._campaigns.map((raw) {
                        final row = Map<String, dynamic>.from(raw as Map);
                        final id = row['id'] as int? ?? 0;
                        final title = (row['title'] as String? ?? '').trim();
                        return DropdownMenuItem<int?>(
                          value: id,
                          child: Text(title.isEmpty ? 'Campagne $id' : title),
                        );
                      }),
                    ],
                    onChanged: _loading || _saving || _extracting
                        ? null
                        : (value) => setState(() => _campaignFilterId = value),
                  ),
                ),
                SizedBox(
                  width: 300,
                  child: DropdownButtonFormField<int>(
                    value: _targetCampaignId,
                    decoration: const InputDecoration(
                      labelText: 'Campagne cible',
                    ),
                    items: _campaigns.map((raw) {
                      final row = Map<String, dynamic>.from(raw as Map);
                      final id = row['id'] as int? ?? 0;
                      final title = (row['title'] as String? ?? '').trim();
                      return DropdownMenuItem<int>(
                        value: id,
                        child: Text(title.isEmpty ? 'Campagne $id' : title),
                      );
                    }).toList(),
                    onChanged: _saving
                        ? null
                        : (value) => setState(() => _targetCampaignId = value),
                  ),
                ),
                FilterChip(
                  label: const Text('Inclure fiches techniques'),
                  selected: _includeTechSheets,
                  onSelected: _loading || _saving || _extracting
                      ? null
                      : (value) {
                          setState(() => _includeTechSheets = value);
                          _load();
                        },
                ),
                FilledButton.icon(
                  onPressed: _extracting || _saving ? null : _extractSelection,
                  icon: _extracting
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.playlist_add_check_circle_outlined),
                  label: const Text('Extraire la selection'),
                ),
                FilledButton.icon(
                  onPressed: _saving || _extracting ? null : _applyReassign,
                  icon: _saving
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.move_down),
                  label: const Text('Affecter a la campagne'),
                ),
              ],
            ),
            const SizedBox(height: 16),
            Expanded(
              child: _loading
                  ? const Center(child: CircularProgressIndicator())
                  : filtered.isEmpty
                      ? const Center(child: Text('Aucun fichier importe'))
                      : ListView.separated(
                          itemCount: filtered.length,
                          separatorBuilder: (_, __) => const SizedBox(height: 10),
                          itemBuilder: (context, index) {
                            final row = Map<String, dynamic>.from(
                              filtered[index] as Map,
                            );
                            final key = _rowKey(row);
                            final selected = _selectedKeys.contains(key);
                            final fileType =
                                (row['file_type'] as String? ?? '').trim();
                            final fileName =
                                (row['file_name'] as String? ?? '').trim();
                            final sender =
                                (row['sender_email'] as String? ?? '').trim();
                            final campaignTitle =
                                (row['campaign_title'] as String? ?? '').trim();
                            final processingMode =
                                (row['processing_mode'] as String? ?? '').trim();
                            final linkedCount = row['linked_items_count'];
                            final campaignId = row['campaign_id'];
                            return Card(
                              child: CheckboxListTile(
                                value: selected,
                                onChanged: (value) {
                                  setState(() {
                                    if (value == true) {
                                      _selectedKeys.add(key);
                                    } else {
                                      _selectedKeys.remove(key);
                                    }
                                  });
                                },
                                controlAffinity:
                                    ListTileControlAffinity.leading,
                                title: Text(
                                  fileName.isEmpty ? key : fileName,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                ),
                                subtitle: Padding(
                                  padding: const EdgeInsets.only(top: 6),
                                  child: Wrap(
                                    spacing: 8,
                                    runSpacing: 8,
                                    children: [
                                      Chip(
                                        label: Text(
                                          fileType == 'tech_sheet'
                                              ? 'Fiche technique'
                                              : 'Facture/Offre',
                                        ),
                                      ),
                                      if (sender.isNotEmpty)
                                        Chip(label: Text(sender)),
                                      Chip(
                                        label: Text(
                                          campaignTitle.isEmpty
                                              ? 'Campagne $campaignId'
                                              : campaignTitle,
                                        ),
                                      ),
                                      if (processingMode.isNotEmpty)
                                        Chip(label: Text(processingMode)),
                                      if (linkedCount != null)
                                        Chip(
                                          label: Text(
                                            'Liens: ${linkedCount.toString()}',
                                          ),
                                        ),
                                    ],
                                  ),
                                ),
                              ),
                            );
                          },
                        ),
            ),
          ],
        ),
      ),
    );
  }
}
