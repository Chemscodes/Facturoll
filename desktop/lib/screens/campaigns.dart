import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:file_picker/file_picker.dart';

import '../api/client.dart';
import '../api/campaigns_api.dart';
import '../api/groups_api.dart';
import '../api/products_api.dart';
import '../api/suppliers_api.dart';
import '../api/templates_api.dart';
import '../config/app_thresholds.dart';
import '../widgets/empty_state.dart';
import '../widgets/html_preview.dart';

class CampaignsScreen extends StatefulWidget {
  const CampaignsScreen({super.key});

  @override
  State<CampaignsScreen> createState() => _CampaignsScreenState();
}

class _CampaignsScreenState extends State<CampaignsScreen> {
  static const Set<String> _productNameKeywords = {
    'autoclave',
    'sterilisateur',
    'thermo',
    'soudeuse',
    'laveur',
    'desinfecteur',
    'panier',
    'pistolet',
    'armoire',
    'compresseur',
    'nebuliseur',
    'otoscope',
    'laryngoscope',
    'tabouret',
    'paravent',
    'gueridon',
    'seringue',
    'aiguille',
    'gants',
    'masque',
    'catheter',
    'canule',
    'perfusion',
    'tubulure',
    'bistouri',
    'consommable',
  };

  static const Set<String> _nonProductTitlePrefixes = {
    'reservoir',
    'utilisable',
    'equip',
    'construction',
    'puissance',
    'taille',
    'chasse',
    'grille',
    'livre',
    'se raccorde',
    'fils',
    'conditions environnementales',
    'cam scanner',
  };

  final Set<String> _confirmingExtractedKeys = <String>{};
  bool _confirmingAllExtracted = false;
  bool _loading = true;
  String? _error;
  List<dynamic> _campaigns = [];
  List<dynamic> _products = [];
  List<dynamic> _groups = [];
  List<dynamic> _suppliers = [];
  List<dynamic> _templates = [];
  Map<String, dynamic>? _selectedCampaign;
  List<dynamic> _campaignItems = [];
  List<dynamic> _attachments = [];
  int _campaignControlTab = 0;
  bool _ocrAnalyzing = false;
  double _ocrProgress = 0.0;
  String _ocrStatus = '';
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

  String _campaignBaseTitle(dynamic raw) {
    if (raw is! Map) return '-';
    final id = raw['id'] is num ? (raw['id'] as num).toInt() : 0;
    final title = (raw['title'] ?? '').toString().trim();
    return title.isEmpty ? 'CAMP $id' : title;
  }

  String _campaignEstablishmentLabel(dynamic raw) {
    if (raw is! Map) return '';
    return (raw['establishment_name'] ?? '').toString().trim();
  }

  DateTime? _ocrStartedAt;
  String _searchQuery = '';
  String _statusFilter = 'ALL';
  final _searchController = TextEditingController();

  final _titleController = TextEditingController();
  final _subjectController = TextEditingController();
  final _messageController = TextEditingController();
  final _priorityController = TextEditingController();
  final _establishmentNameController = TextEditingController();
  final _lotReferenceController = TextEditingController();
  final _establishmentAddressController = TextEditingController();
  final _establishmentPhoneController = TextEditingController();
  final _pharmacyServiceController = TextEditingController();
  final _marketOfficeServiceController = TextEditingController();
  final _establishmentTypeController = TextEditingController();
  final _depositDateController = TextEditingController();
  bool _processingInProgress = false;
  bool _processingValidated = false;
  double _displayEmbeddingThreshold = AppThresholds.minExtractionThreshold;
  Map<String, dynamic> _ocrExtractionConfig = {
    'useConstraint': true,
    'expectedProductCount': null,
    'expectedProductCounts': <String, int>{},
    'strictExpectedCount': true,
    'strictCountMode': 'strict',
    'documentTypeHint': 'mixed',
    'layoutHint': 'auto',
    'requireSpecsForAcceptance': false,
  };

  void _showError(Object err) {
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
  }

  void _startCountdownTicker() {
    _countdownTicker?.cancel();
    _countdownTicker = Timer.periodic(const Duration(minutes: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void initState() {
    super.initState();
    _startCountdownTicker();
    _load();
  }

  @override
  void dispose() {
    _titleController.dispose();
    _subjectController.dispose();
    _messageController.dispose();
    _priorityController.dispose();
    _establishmentNameController.dispose();
    _lotReferenceController.dispose();
    _establishmentAddressController.dispose();
    _establishmentPhoneController.dispose();
    _pharmacyServiceController.dispose();
    _marketOfficeServiceController.dispose();
    _establishmentTypeController.dispose();
    _depositDateController.dispose();
    _searchController.dispose();
    _countdownTicker?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      _campaigns = await CampaignsApi.list();
      _products = await ProductsApi.list();
      _groups = await GroupsApi.list();
      _suppliers = await SuppliersApi.list();
      _templates = await TemplatesApi.list();
      if (_campaigns.isNotEmpty) {
        await _selectCampaign(_campaigns.first as Map<String, dynamic>);
      }
    } catch (err) {
      _error = ApiClient.formatError(err);
    }
    if (mounted) {
      setState(() => _loading = false);
    }
  }

  List<dynamic> get _filteredCampaigns {
    final query = _searchQuery.trim().toLowerCase();
    final filtered = _campaigns.where((c) {
      final status = (c['status'] ?? '').toString();
      final matchesStatus = _statusFilter == 'ALL' || status == _statusFilter;
      if (!matchesStatus) return false;
      if (query.isEmpty) return true;
      final title = (c['title'] ?? '').toString().toLowerCase();
      final subject = (c['subject'] ?? '').toString().toLowerCase();
      return title.contains(query) || subject.contains(query);
    }).toList();

    filtered.sort((a, b) {
      final aTitle = (a['title'] ?? '').toString().toLowerCase();
      final bTitle = (b['title'] ?? '').toString().toLowerCase();
      return aTitle.compareTo(bTitle);
    });

    return filtered;
  }

  bool get _isSelectedCampaignClosed =>
      (_selectedCampaign?['status']?.toString().toUpperCase() ?? '') ==
      'CLOSED';

  String _ensureFileExtension(String path, String extension) {
    final ext = extension.startsWith('.') ? extension : '.$extension';
    if (path.toLowerCase().endsWith(ext.toLowerCase())) {
      return path;
    }
    return '$path$ext';
  }

  String _formatDateOnly(DateTime? value) {
    if (value == null) return '';
    final local = value.toLocal();
    final month = local.month.toString().padLeft(2, '0');
    final day = local.day.toString().padLeft(2, '0');
    return '${local.year}-$month-$day';
  }

  DateTime? _parseDateOnly(String raw) {
    final text = raw.trim();
    if (text.isEmpty) return null;
    final normalized = text.replaceAll('/', '-');
    final parts = normalized.split('-');
    if (parts.length == 3) {
      final year = int.tryParse(parts[0]);
      final month = int.tryParse(parts[1]);
      final day = int.tryParse(parts[2]);
      if (year != null && month != null && day != null) {
        return DateTime(year, month, day);
      }
    }
    return DateTime.tryParse(text);
  }

  String _countdownLabel(DateTime? depositDate) {
    if (depositDate == null) return 'Date depot non renseignee';
    final now = DateTime.now();
    final target = DateTime(
      depositDate.year,
      depositDate.month,
      depositDate.day,
      23,
      59,
      59,
    );
    final diff = target.difference(now);
    if (diff.isNegative) {
      final late = diff.abs();
      final days = late.inDays;
      final hours = late.inHours.remainder(24);
      final minutes = late.inMinutes.remainder(60);
      return 'Depasse de ${days}j ${hours}h ${minutes}m';
    }
    final days = diff.inDays;
    final hours = diff.inHours.remainder(24);
    final minutes = diff.inMinutes.remainder(60);
    return 'Dans ${days}j ${hours}h ${minutes}m';
  }

  ({Color background, Color border, Color text}) _countdownColors(
    DateTime? depositDate,
  ) {
    if (depositDate == null) {
      return (
        background: Colors.blueGrey.withValues(alpha: 0.08),
        border: Colors.blueGrey.withValues(alpha: 0.18),
        text: const Color(0xFF334155),
      );
    }
    final now = DateTime.now();
    final target = DateTime(
      depositDate.year,
      depositDate.month,
      depositDate.day,
      23,
      59,
      59,
    ).toLocal();
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

  Widget _countdownBadge(DateTime? depositDate) {
    final colors = _countdownColors(depositDate);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
      decoration: BoxDecoration(
        color: colors.background,
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: colors.border),
      ),
      child: Text(
        _countdownLabel(depositDate),
        style: TextStyle(
          fontSize: 11,
          fontWeight: FontWeight.w700,
          color: colors.text,
        ),
      ),
    );
  }

  Widget _statusBadge(String status) {
    final normalized = status.trim().toUpperCase();
    final Color background;
    final Color border;
    final Color text;
    switch (normalized) {
      case 'CLOSED':
        background = const Color(0xFFF2F4F7);
        border = const Color(0xFFD0D5DD);
        text = const Color(0xFF344054);
        break;
      case 'SENT':
        background = const Color(0xFFE8F5E9);
        border = const Color(0xFF9AD1A4);
        text = const Color(0xFF1B4332);
        break;
      case 'SENDING':
        background = const Color(0xFFFFF8D6);
        border = const Color(0xFFE7C85B);
        text = const Color(0xFF8A6D00);
        break;
      default:
        background = const Color(0xFFEFF4FF);
        border = const Color(0xFFB2CCFF);
        text = const Color(0xFF175CD3);
        break;
    }
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: border),
      ),
      child: Text(
        normalized,
        style: TextStyle(
          fontSize: 11,
          fontWeight: FontWeight.w700,
          color: text,
        ),
      ),
    );
  }

  Widget _buildCampaignListTile(Map<String, dynamic> campaign) {
    final selected = _selectedCampaign?['id'] == campaign['id'];
    final depositDate = DateTime.tryParse(
      (campaign['deposit_date'] ?? '').toString(),
    );
    final establishment = _campaignEstablishmentLabel(campaign);
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      elevation: selected ? 0 : 1,
      color: selected ? const Color(0xFFEFF4FF) : null,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(14),
        side: BorderSide(
          color: selected ? const Color(0xFFB2CCFF) : const Color(0xFFE4E7EC),
        ),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(14),
        onTap: () => _selectCampaign(campaign),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                _campaignBaseTitle(campaign),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  fontWeight: FontWeight.w700,
                  color: Color(0xFF101828),
                ),
              ),
              if (establishment.isNotEmpty) ...[
                const SizedBox(height: 4),
                Text(
                  establishment,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    fontSize: 12,
                    color: Color(0xFF475467),
                  ),
                ),
              ],
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  _statusBadge((campaign['status'] ?? '').toString()),
                  _countdownBadge(depositDate),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildCampaignsSidebar({
    required List<dynamic> campaigns,
    required bool compact,
  }) {
    final sidebar = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const Text('Campaigns',
                style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
            const Spacer(),
            IconButton(onPressed: _createCampaign, icon: const Icon(Icons.add)),
            IconButton(onPressed: _load, icon: const Icon(Icons.refresh)),
          ],
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _searchController,
          decoration: InputDecoration(
            labelText: 'Search campaigns',
            prefixIcon: const Icon(Icons.search),
            suffixIcon: _searchQuery.isEmpty
                ? null
                : IconButton(
                    icon: const Icon(Icons.clear),
                    onPressed: () {
                      _searchController.clear();
                      setState(() => _searchQuery = '');
                    },
                  ),
          ),
          onChanged: (value) => setState(() => _searchQuery = value),
        ),
        const SizedBox(height: 8),
        DropdownButtonFormField<String>(
          value: _statusFilter,
          decoration: const InputDecoration(labelText: 'Status'),
          items: const [
            DropdownMenuItem(value: 'ALL', child: Text('All statuses')),
            DropdownMenuItem(value: 'DRAFT', child: Text('DRAFT')),
            DropdownMenuItem(value: 'SENDING', child: Text('SENDING')),
            DropdownMenuItem(value: 'SENT', child: Text('SENT')),
            DropdownMenuItem(value: 'CLOSED', child: Text('CLOSED')),
          ],
          onChanged: (value) => setState(() => _statusFilter = value ?? 'ALL'),
        ),
        const SizedBox(height: 12),
        Expanded(
          child: campaigns.isEmpty
              ? EmptyState(
                  icon: Icons.campaign_outlined,
                  title: _campaigns.isEmpty
                      ? 'No campaigns yet'
                      : 'No campaigns match filters',
                  message: _campaigns.isEmpty
                      ? 'Create a campaign to start sending requests.'
                      : 'Try a different keyword or status.',
                  action: _campaigns.isEmpty
                      ? ElevatedButton(
                          onPressed: _createCampaign,
                          child: const Text('Create campaign'),
                        )
                      : null,
                )
              : ListView.builder(
                  itemCount: campaigns.length,
                  itemBuilder: (context, index) => _buildCampaignListTile(
                    campaigns[index] as Map<String, dynamic>,
                  ),
                ),
        ),
      ],
    );

    if (compact) {
      return Card(
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: sidebar,
        ),
      );
    }

    return SizedBox(
      width: 340,
      child: sidebar,
    );
  }

  Widget _buildCampaignDetailPanel() {
    return _selectedCampaign == null
        ? EmptyState(
            icon: Icons.campaign_outlined,
            title: 'Select a campaign',
            message: 'Choose a campaign on the left or create a new one.',
            action: ElevatedButton(
              onPressed: _createCampaign,
              child: const Text('Create campaign'),
            ),
          )
        : Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Flexible(
                fit: FlexFit.loose,
                child: SingleChildScrollView(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      TextField(
                        controller: _titleController,
                        decoration:
                            const InputDecoration(labelText: 'Campaign name'),
                      ),
                      const SizedBox(height: 8),
                      Align(
                        alignment: Alignment.centerLeft,
                        child: Chip(
                          label: Text(
                            'Status: ${_selectedCampaign?['status'] ?? 'DRAFT'}',
                          ),
                        ),
                      ),
                      _campaignMetaCard(_selectedCampaign),
                      if (_ocrAnalyzing) ...[
                        const SizedBox(height: 12),
                        Container(
                          width: double.infinity,
                          padding: const EdgeInsets.all(12),
                          decoration: BoxDecoration(
                            color: Colors.orange.shade50,
                            border: Border.all(color: Colors.orange.shade200),
                            borderRadius: BorderRadius.circular(8),
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              const Text(
                                'Traitement en cours...',
                                style: TextStyle(fontWeight: FontWeight.w700),
                              ),
                              const SizedBox(height: 6),
                              LinearProgressIndicator(
                                value: _ocrProgress > 0 ? _ocrProgress : null,
                              ),
                              const SizedBox(height: 6),
                              Text(
                                _ocrStartedAt == null
                                    ? _ocrStatus
                                    : '$_ocrStatus - ${DateTime.now().difference(_ocrStartedAt!).inSeconds}s',
                                style: const TextStyle(
                                  fontSize: 12,
                                  color: Colors.black87,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ],
                      const SizedBox(height: 12),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          ChoiceChip(
                            label: const Text('Edition'),
                            selected: _campaignControlTab == 0,
                            onSelected: (_) => setState(() {
                              _campaignControlTab = 0;
                            }),
                          ),
                          ChoiceChip(
                            label: const Text('Extraction'),
                            selected: _campaignControlTab == 1,
                            onSelected: (_) => setState(() {
                              _campaignControlTab = 1;
                            }),
                          ),
                          ChoiceChip(
                            label: const Text('Envoi'),
                            selected: _campaignControlTab == 2,
                            onSelected: (_) => setState(() {
                              _campaignControlTab = 2;
                            }),
                          ),
                          ChoiceChip(
                            label:
                                Text('Pieces jointes (${_attachments.length})'),
                            selected: _campaignControlTab == 3,
                            onSelected: (_) => setState(() {
                              _campaignControlTab = 3;
                            }),
                          ),
                        ],
                      ),
                      const SizedBox(height: 10),
                      if (_campaignControlTab == 0) ...[
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            ElevatedButton(
                              onPressed: _ocrAnalyzing ? null : _saveCampaign,
                              child: const Text('Save'),
                            ),
                            OutlinedButton(
                              onPressed: _ocrAnalyzing ? null : _manageItems,
                              child: const Text('Items'),
                            ),
                            OutlinedButton(
                              onPressed:
                                  _ocrAnalyzing ? null : _manageRecipients,
                              child: const Text('Recipients'),
                            ),
                            OutlinedButton.icon(
                              onPressed:
                                  _ocrAnalyzing ? null : _openEmailDetailsModal,
                              icon: const Icon(Icons.email_outlined),
                              label: const Text('Details email'),
                            ),
                            OutlinedButton.icon(
                              onPressed:
                                  _ocrAnalyzing ? null : _exportCampaignExcel,
                              icon: const Icon(Icons.table_view_outlined),
                              label: const Text('Exporter Excel'),
                            ),
                            OutlinedButton.icon(
                              onPressed:
                                  _ocrAnalyzing || _isSelectedCampaignClosed
                                      ? null
                                      : _closeCampaign,
                              icon: const Icon(Icons.lock_outline),
                              label: const Text('Cloturer'),
                            ),
                            OutlinedButton.icon(
                              onPressed:
                                  _ocrAnalyzing || !_isSelectedCampaignClosed
                                      ? null
                                      : _purgeCampaign,
                              icon: const Icon(Icons.delete_forever_outlined),
                              label: const Text('Purger'),
                            ),
                          ],
                        ),
                        const SizedBox(height: 12),
                        TextField(
                          controller: _establishmentNameController,
                          decoration: const InputDecoration(
                            labelText: 'Nom etablissement',
                          ),
                        ),
                        const SizedBox(height: 8),
                        TextField(
                          controller: _lotReferenceController,
                          decoration: const InputDecoration(
                            labelText: 'Lot',
                          ),
                        ),
                        const SizedBox(height: 8),
                        TextField(
                          controller: _establishmentAddressController,
                          decoration: const InputDecoration(
                            labelText: 'Adresse',
                          ),
                          maxLines: 2,
                        ),
                        const SizedBox(height: 8),
                        TextField(
                          controller: _establishmentPhoneController,
                          decoration: const InputDecoration(
                            labelText: 'Telephone',
                          ),
                        ),
                        const SizedBox(height: 8),
                        TextField(
                          controller: _pharmacyServiceController,
                          decoration: const InputDecoration(
                            labelText: 'Service pharmacie',
                          ),
                        ),
                        const SizedBox(height: 8),
                        TextField(
                          controller: _marketOfficeServiceController,
                          decoration: const InputDecoration(
                            labelText: 'Service bureau de marche',
                          ),
                        ),
                        const SizedBox(height: 8),
                        TextField(
                          controller: _establishmentTypeController,
                          decoration: const InputDecoration(
                            labelText: 'Type d\'etablissement',
                          ),
                        ),
                        const SizedBox(height: 8),
                        TextField(
                          controller: _depositDateController,
                          decoration: const InputDecoration(
                            labelText: 'Date de depot',
                            hintText: 'YYYY-MM-DD',
                          ),
                        ),
                        CheckboxListTile(
                          contentPadding: EdgeInsets.zero,
                          value: _processingInProgress,
                          onChanged: (value) => setState(() {
                            _processingInProgress = value ?? false;
                          }),
                          title: const Text('En cours de traitement'),
                        ),
                        CheckboxListTile(
                          contentPadding: EdgeInsets.zero,
                          value: _processingValidated,
                          onChanged: (value) => setState(() {
                            _processingValidated = value ?? false;
                          }),
                          title: const Text('Valide'),
                        ),
                      ],
                      if (_campaignControlTab == 1) ...[
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            OutlinedButton.icon(
                              onPressed:
                                  _ocrAnalyzing ? null : _addPdfAttachments,
                              icon: const Icon(Icons.attach_file),
                              label: const Text('Attach PDF'),
                            ),
                            OutlinedButton.icon(
                              onPressed: _ocrAnalyzing || _attachments.isEmpty
                                  ? null
                                  : _extractAttachmentsOnce,
                              icon: const Icon(Icons.auto_fix_high),
                              label: Text(
                                _hasPendingAttachmentOcr
                                    ? 'Analyser les fichiers'
                                    : 'Reanalyser les fichiers',
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 12),
                        if (_selectedCampaignProcessingMode() ==
                            'standard') ...[
                          const Text(
                            'Type d\'analyse',
                            style: TextStyle(fontWeight: FontWeight.w700),
                          ),
                          const SizedBox(height: 8),
                          Wrap(
                            spacing: 8,
                            runSpacing: 8,
                            children: [
                              ChoiceChip(
                                label: const Text('Mixte'),
                                selected:
                                    ((_ocrExtractionConfig['documentTypeHint']
                                                as String?) ??
                                            'mixed') ==
                                        'mixed',
                                onSelected: _ocrAnalyzing
                                    ? null
                                    : (_) {
                                        setState(() {
                                          _ocrExtractionConfig =
                                              _applyStandardAnalysisType(
                                            _ocrExtractionConfig,
                                            'mixed',
                                          );
                                        });
                                      },
                              ),
                              ChoiceChip(
                                label: const Text('Cahier des charges'),
                                selected:
                                    ((_ocrExtractionConfig['documentTypeHint']
                                                as String?) ??
                                            'mixed') ==
                                        'products_with_specs',
                                onSelected: _ocrAnalyzing
                                    ? null
                                    : (_) {
                                        setState(() {
                                          _ocrExtractionConfig =
                                              _applyStandardAnalysisType(
                                            _ocrExtractionConfig,
                                            'products_with_specs',
                                          );
                                        });
                                      },
                              ),
                              ChoiceChip(
                                label: const Text('Offre simple'),
                                selected:
                                    ((_ocrExtractionConfig['documentTypeHint']
                                                as String?) ??
                                            'mixed') ==
                                        'simple_list',
                                onSelected: _ocrAnalyzing
                                    ? null
                                    : (_) {
                                        setState(() {
                                          _ocrExtractionConfig =
                                              _applyStandardAnalysisType(
                                            _ocrExtractionConfig,
                                            'simple_list',
                                          );
                                        });
                                      },
                              ),
                            ],
                          ),
                        ] else ...[
                          Container(
                            width: double.infinity,
                            padding: const EdgeInsets.all(12),
                            decoration: BoxDecoration(
                              color: const Color(0xFFF4F6F8),
                              borderRadius: BorderRadius.circular(10),
                              border:
                                  Border.all(color: const Color(0xFFD7DEE5)),
                            ),
                            child: const Text('Profil: Liste produits stricte'),
                          ),
                        ],
                        const SizedBox(height: 12),
                        const Text(
                          'Structure dominante',
                          style: TextStyle(fontWeight: FontWeight.w700),
                        ),
                        const SizedBox(height: 8),
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            ChoiceChip(
                              label: const Text('Auto'),
                              selected: ((_ocrExtractionConfig['layoutHint']
                                          as String?) ??
                                      'auto') ==
                                  'auto',
                              onSelected: _ocrAnalyzing
                                  ? null
                                  : (_) {
                                      setState(() {
                                        _ocrExtractionConfig = {
                                          ..._ocrExtractionConfig,
                                          'layoutHint': 'auto',
                                        };
                                      });
                                    },
                            ),
                            ChoiceChip(
                              label: const Text('Tableau/Liste'),
                              selected: ((_ocrExtractionConfig['layoutHint']
                                          as String?) ??
                                      'auto') ==
                                  'table',
                              onSelected: _ocrAnalyzing
                                  ? null
                                  : (_) {
                                      setState(() {
                                        _ocrExtractionConfig = {
                                          ..._ocrExtractionConfig,
                                          'layoutHint': 'table',
                                        };
                                      });
                                    },
                            ),
                            ChoiceChip(
                              label: const Text('Paragraphe'),
                              selected: ((_ocrExtractionConfig['layoutHint']
                                          as String?) ??
                                      'auto') ==
                                  'paragraph',
                              onSelected: _ocrAnalyzing
                                  ? null
                                  : (_) {
                                      setState(() {
                                        _ocrExtractionConfig = {
                                          ..._ocrExtractionConfig,
                                          'layoutHint': 'paragraph',
                                        };
                                      });
                                    },
                            ),
                            ChoiceChip(
                              label: const Text('Mixte'),
                              selected: ((_ocrExtractionConfig['layoutHint']
                                          as String?) ??
                                      'auto') ==
                                  'mixed',
                              onSelected: _ocrAnalyzing
                                  ? null
                                  : (_) {
                                      setState(() {
                                        _ocrExtractionConfig = {
                                          ..._ocrExtractionConfig,
                                          'layoutHint': 'mixed',
                                        };
                                      });
                                    },
                            ),
                          ],
                        ),
                        const SizedBox(height: 8),
                        Text(
                          _documentConfigSummaryForMode(
                            _ocrExtractionConfig,
                            _selectedCampaignProcessingMode(),
                          ),
                          style: const TextStyle(
                            color: Colors.black54,
                            fontSize: 12,
                          ),
                        ),
                      ],
                      if (_campaignControlTab == 2) ...[
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            OutlinedButton(
                              onPressed: _ocrAnalyzing ? null : _previewEmail,
                              child: const Text('Preview email'),
                            ),
                            ElevatedButton(
                              onPressed:
                                  _ocrAnalyzing || _isSelectedCampaignClosed
                                      ? null
                                      : _sendCampaign,
                              child: const Text('Send'),
                            ),
                          ],
                        ),
                      ],
                      if (_campaignControlTab == 3) ...[
                        if (_attachments.isEmpty)
                          const Text(
                            'No PDF attached. Add one before sending if needed.',
                            style: TextStyle(color: Colors.black54),
                          )
                        else
                          Container(
                            constraints: const BoxConstraints(maxHeight: 180),
                            child: ListView(
                              shrinkWrap: true,
                              children: _attachments.map((a) {
                                final size = (a['file_size'] ?? 0) as num;
                                final mb = (size / (1024 * 1024));
                                return ListTile(
                                  dense: true,
                                  leading: const Icon(
                                    Icons.picture_as_pdf,
                                    color: Colors.red,
                                  ),
                                  title: Text(
                                    a['file_name']?.toString() ??
                                        'attachment.pdf',
                                  ),
                                  subtitle: Text(
                                    '${(() {
                                      final preview =
                                          _attachmentPreviewAsMap(a);
                                      if (preview != null &&
                                          preview['embedding_threshold']
                                              is num) {
                                        final v =
                                            (preview['embedding_threshold']
                                                        as num)
                                                    .toDouble() *
                                                100;
                                        return 'emb: ${v.toStringAsFixed(0)}% - ';
                                      }
                                      return '';
                                    })()}'
                                    '${mb.toStringAsFixed(2)} MB'
                                    ' - OCR: ${a['parse_engine'] ?? '-'}'
                                    ' - score: ${(((a['parse_score'] ?? 0) as num) * 100).toStringAsFixed(1)}%',
                                  ),
                                  trailing: IconButton(
                                    icon: const Icon(Icons.delete_outline),
                                    onPressed: _ocrAnalyzing
                                        ? null
                                        : () =>
                                            _deleteAttachment(a['id'] as int),
                                  ),
                                );
                              }).toList(),
                            ),
                          ),
                      ],
                      const SizedBox(height: 12),
                      Row(
                        children: [
                          const Text(
                            'Produits extraits et produits demandes',
                            style: TextStyle(fontWeight: FontWeight.bold),
                          ),
                          const Spacer(),
                          OutlinedButton.icon(
                            onPressed: _openExtractedItemsFullscreen,
                            icon: const Icon(Icons.open_in_full),
                            label: const Text('Agrandir'),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Expanded(
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Expanded(
                      flex: 3,
                      child: _buildExtractedItemsPane(
                        showExpandButton: true,
                        namesOnly: true,
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      flex: 2,
                      child: _buildCampaignRequestedItemsPane(),
                    ),
                  ],
                ),
              ),
            ],
          );
  }

  Widget _campaignMetaCard(Map<String, dynamic>? campaign) {
    if (campaign == null) return const SizedBox.shrink();
    final depositDate = DateTime.tryParse(
      (campaign['deposit_date'] ?? '').toString(),
    );
    final countdownColors = _countdownColors(depositDate);
    final chips = <Widget>[
      _metaChip(
        'Etablissement',
        (campaign['establishment_name'] ?? '-').toString(),
      ),
      _metaChip('Lot', (campaign['lot_reference'] ?? '-').toString()),
      _metaChip('Type', (campaign['establishment_type'] ?? '-').toString()),
      _metaChip(
        'Statut traitement',
        [
          if (campaign['processing_in_progress'] == true) 'En cours',
          if (campaign['processing_validated'] == true) 'Valide',
        ].join(' / ').trim().isEmpty
            ? '-'
            : [
                if (campaign['processing_in_progress'] == true) 'En cours',
                if (campaign['processing_validated'] == true) 'Valide',
              ].join(' / '),
      ),
      _metaChip(
        'Date depot',
        depositDate == null ? '-' : _formatDateOnly(depositDate),
      ),
      _metaChip(
        'Compte a rebours',
        _countdownLabel(depositDate),
        backgroundColor: countdownColors.background,
        borderColor: countdownColors.border,
        textColor: countdownColors.text,
      ),
    ];

    return Card(
      margin: const EdgeInsets.only(top: 8),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: chips,
            ),
            const SizedBox(height: 10),
            Text(
              'Adresse: ${(campaign['establishment_address'] ?? '-').toString()}',
            ),
            const SizedBox(height: 4),
            Text(
              'Telephone: ${(campaign['establishment_phone'] ?? '-').toString()} | Pharmacie: ${(campaign['pharmacy_service'] ?? '-').toString()} | Bureau de marche: ${(campaign['market_office_service'] ?? '-').toString()}',
            ),
          ],
        ),
      ),
    );
  }

  Widget _metaChip(
    String label,
    String value, {
    Color? backgroundColor,
    Color? borderColor,
    Color? textColor,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: backgroundColor ?? Colors.blueGrey.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(
          color: borderColor ?? Colors.blueGrey.withValues(alpha: 0.18),
        ),
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

  String _suggestedCampaignTitle() {
    var maxId = 0;
    for (final row in _campaigns) {
      if (row is! Map) continue;
      final raw = row['id'];
      final n = int.tryParse((raw ?? '').toString());
      if (n != null && n > maxId) {
        maxId = n;
      }
    }
    final next = maxId > 0 ? maxId + 1 : (_campaigns.length + 1);
    return 'CAMP $next';
  }

  Future<void> _selectCampaign(Map<String, dynamic> campaign) async {
    _selectedCampaign = campaign;
    _campaignControlTab = 0;
    _titleController.text = campaign['title'] ?? '';
    _subjectController.text = campaign['subject'] ?? '';
    _messageController.text = campaign['message_html'] ?? '';
    _priorityController.text = (campaign['priority'] ?? 0).toString();
    _establishmentNameController.text =
        (campaign['establishment_name'] ?? '').toString();
    _lotReferenceController.text = (campaign['lot_reference'] ?? '').toString();
    _establishmentAddressController.text =
        (campaign['establishment_address'] ?? '').toString();
    _establishmentPhoneController.text =
        (campaign['establishment_phone'] ?? '').toString();
    _pharmacyServiceController.text =
        (campaign['pharmacy_service'] ?? '').toString();
    _marketOfficeServiceController.text =
        (campaign['market_office_service'] ?? '').toString();
    _establishmentTypeController.text =
        (campaign['establishment_type'] ?? '').toString();
    _depositDateController.text = _formatDateOnly(
      DateTime.tryParse((campaign['deposit_date'] ?? '').toString()),
    );
    _processingInProgress = campaign['processing_in_progress'] == true;
    _processingValidated = campaign['processing_validated'] == true;
    await _loadCampaignItems();
    await _loadAttachments();
    setState(() {});
  }

  Future<void> _loadCampaignItems() async {
    if (_selectedCampaign == null) return;
    try {
      _campaignItems =
          await CampaignsApi.items(_selectedCampaign!['id'] as int);
    } catch (err) {
      _campaignItems = [];
      if (mounted) {
        _showError(err);
      }
    }
  }

  Future<void> _loadAttachments() async {
    if (_selectedCampaign == null) return;
    try {
      _attachments =
          await CampaignsApi.attachments(_selectedCampaign!['id'] as int);
    } catch (err) {
      _attachments = [];
      if (mounted) {
        _showError(err);
      }
    }
  }

  bool _attachmentNeedsExtraction(dynamic raw) {
    if (raw is! Map<String, dynamic>) return false;
    if ((raw['parse_engine'] ?? '').toString().trim().toLowerCase() ==
        'processing') {
      return true;
    }
    return raw['extracted_at'] == null || raw['extraction_preview'] == null;
  }

  Future<void> _waitForAttachmentExtractionCompletion(
    List<int> attachmentIds,
  ) async {
    if (attachmentIds.isEmpty || _selectedCampaign == null) return;
    final deadline = DateTime.now().add(const Duration(minutes: 12));
    while (DateTime.now().isBefore(deadline)) {
      await Future.delayed(const Duration(seconds: 2));
      await _loadAttachments();
      if (!mounted) return;
      final targetRows = _attachments
          .whereType<Map<String, dynamic>>()
          .where((row) {
            final rawId = row['id'];
            final id = rawId is num ? rawId.toInt() : null;
            return id != null && attachmentIds.contains(id);
          })
          .toList();
      final completedCount =
          targetRows.where((row) => !_attachmentNeedsExtraction(row)).length;
      setState(() {
        _ocrProgress = targetRows.isEmpty
            ? 0.0
            : completedCount / targetRows.length;
        _ocrStatus =
            'Analyse des fichiers... $completedCount/${targetRows.length}';
      });
      if (targetRows.isNotEmpty &&
          targetRows.every((row) => !_attachmentNeedsExtraction(row))) {
        return;
      }
    }
    throw Exception(
      'Timeout extraction: le traitement serveur est toujours en cours.',
    );
  }

  bool get _hasPendingAttachmentOcr {
    for (final raw in _attachments) {
      if (raw is! Map<String, dynamic>) continue;
      final extractedAt = raw['extracted_at'];
      final preview = _attachmentPreviewAsMap(raw);
      if (extractedAt == null) return true;
      if (preview == null || preview.isEmpty) return true;
    }
    return false;
  }

  String _selectedCampaignProcessingMode() {
    final value = (_selectedCampaign?['processing_mode'] ?? '')
        .toString()
        .trim()
        .toLowerCase();
    return value == 'strict' ? 'strict' : 'standard';
  }

  Map<String, dynamic>? _attachmentPreviewAsMap(Map<String, dynamic> raw) {
    if (_selectedCampaignProcessingMode() == 'strict') {
      final strictPreview = _previewAsMap(raw['strict_extraction_preview']);
      if (strictPreview != null && strictPreview.isNotEmpty) {
        return strictPreview;
      }
    }
    return _previewAsMap(raw['extraction_preview']);
  }

  Map<String, dynamic>? _previewAsMap(dynamic raw) {
    if (raw is Map<String, dynamic>) return raw;
    if (raw is Map) {
      return raw.map((k, v) => MapEntry(k.toString(), v));
    }
    if (raw is String) {
      final text = raw.trim();
      if (text.isEmpty) return null;
      try {
        final decoded = jsonDecode(text);
        if (decoded is Map<String, dynamic>) return decoded;
        if (decoded is Map) {
          return decoded.map((k, v) => MapEntry(k.toString(), v));
        }
      } catch (_) {
        return null;
      }
    }
    return null;
  }

  Future<void> _saveCampaign() async {
    if (_selectedCampaign == null) return;
    final payload = {
      'title': _titleController.text.trim(),
      'subject': _subjectController.text.trim(),
      'message_html': _messageController.text.trim(),
      'priority': int.tryParse(_priorityController.text.trim()) ?? 0,
      'establishment_name': _establishmentNameController.text.trim(),
      'lot_reference': _lotReferenceController.text.trim(),
      'establishment_address': _establishmentAddressController.text.trim(),
      'establishment_phone': _establishmentPhoneController.text.trim(),
      'pharmacy_service': _pharmacyServiceController.text.trim(),
      'market_office_service': _marketOfficeServiceController.text.trim(),
      'establishment_type': _establishmentTypeController.text.trim(),
      'deposit_date':
          _parseDateOnly(_depositDateController.text)?.toIso8601String(),
      'processing_in_progress': _processingInProgress,
      'processing_validated': _processingValidated,
    };
    try {
      final updated =
          await CampaignsApi.update(_selectedCampaign!['id'] as int, payload);
      await _load();
      await _selectCampaign(updated);
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  Future<void> _createCampaign() async {
    final suggestedTitle = _suggestedCampaignTitle();
    final establishmentNameController = TextEditingController();
    final lotReferenceController = TextEditingController();
    final establishmentAddressController = TextEditingController();
    final establishmentPhoneController = TextEditingController();
    final pharmacyServiceController = TextEditingController();
    final marketOfficeServiceController = TextEditingController();
    final establishmentTypeController = TextEditingController();
    final depositDateController = TextEditingController();
    final titleController = TextEditingController(text: suggestedTitle);
    final subjectController = TextEditingController();
    final messageController =
        TextEditingController(text: '<p>Hello, please share your offer.</p>');
    final priorityController = TextEditingController(text: '0');
    int? selectedTemplateId;
    var currentStep = 0;
    var processingInProgress = false;
    var processingValidated = false;
    var processingMode = 'standard';
    final selectedSupplierIds = <int>{};
    final attachmentPaths = <String>[];
    var wizardOcrConfig = _normalizedOcrExtractionConfig(_ocrExtractionConfig);
    String recipientSearch = '';
    final templatesById = {
      for (final t in _templates) t['id'] as int: t as Map<String, dynamic>,
    };
    final groupMembers = <int, Set<int>>{};

    bool? groupValue(Set<int> members) {
      if (members.isEmpty) return false;
      final selectedCount = members.where(selectedSupplierIds.contains).length;
      if (selectedCount == 0) return false;
      if (selectedCount == members.length) return true;
      return null;
    }

    void toggleGroupSelection(Set<int> members) {
      if (members.isEmpty) return;
      final current = groupValue(members);
      if (current == true) {
        selectedSupplierIds.removeAll(members);
      } else {
        selectedSupplierIds.addAll(members);
      }
    }

    bool canContinueFrom(int step) {
      switch (step) {
        case 0:
          return true;
        case 1:
          return titleController.text.trim().isNotEmpty &&
              subjectController.text.trim().isNotEmpty &&
              messageController.text.trim().isNotEmpty;
        case 2:
          return selectedSupplierIds.isNotEmpty;
        default:
          return true;
      }
    }

    try {
      final memberEntries = await Future.wait(
        _groups.map((group) async {
          final groupId = group['id'] as int;
          final members = await GroupsApi.listMembers(groupId);
          return MapEntry(
            groupId,
            members.map<int>((m) => m['supplier_id'] as int).toSet(),
          );
        }),
      );
      for (final entry in memberEntries) {
        groupMembers[entry.key] = entry.value;
      }
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
      titleController.dispose();
      establishmentNameController.dispose();
      lotReferenceController.dispose();
      establishmentAddressController.dispose();
      establishmentPhoneController.dispose();
      pharmacyServiceController.dispose();
      marketOfficeServiceController.dispose();
      establishmentTypeController.dispose();
      depositDateController.dispose();
      subjectController.dispose();
      messageController.dispose();
      priorityController.dispose();
      return;
    }

    if (!mounted) {
      titleController.dispose();
      establishmentNameController.dispose();
      lotReferenceController.dispose();
      establishmentAddressController.dispose();
      establishmentPhoneController.dispose();
      pharmacyServiceController.dispose();
      marketOfficeServiceController.dispose();
      establishmentTypeController.dispose();
      depositDateController.dispose();
      subjectController.dispose();
      messageController.dispose();
      priorityController.dispose();
      return;
    }

    final result = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setStateDialog) {
          final filteredSuppliers = _suppliers.where((raw) {
            final name = (raw['name'] ?? '').toString().toLowerCase();
            final email = (raw['email'] ?? '').toString().toLowerCase();
            final q = recipientSearch.trim().toLowerCase();
            return q.isEmpty || name.contains(q) || email.contains(q);
          }).toList();

          Widget stepContent;
          if (currentStep == 0) {
            stepContent = Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'Etape 1/5 - Informations campagne',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 12),
                const Text(
                  'Mode de traitement',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    ChoiceChip(
                      label: const Text('Standard'),
                      selected: processingMode == 'standard',
                      onSelected: (_) {
                        setStateDialog(() {
                          processingMode = 'standard';
                          wizardOcrConfig = _applyProcessingModeDefaults(
                            wizardOcrConfig,
                            processingMode,
                          );
                        });
                      },
                    ),
                    ChoiceChip(
                      label: const Text('Strict'),
                      selected: processingMode == 'strict',
                      onSelected: (_) {
                        setStateDialog(() {
                          processingMode = 'strict';
                          wizardOcrConfig = _applyProcessingModeDefaults(
                            wizardOcrConfig,
                            processingMode,
                          );
                        });
                      },
                    ),
                  ],
                ),
                if (processingMode == 'standard') ...[
                  const SizedBox(height: 12),
                  const Text(
                    'Type d\'analyse',
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      ChoiceChip(
                        label: const Text('Mixte'),
                        selected:
                            ((wizardOcrConfig['documentTypeHint'] as String?) ??
                                    'mixed') ==
                                'mixed',
                        onSelected: (_) {
                          setStateDialog(() {
                            wizardOcrConfig = _applyStandardAnalysisType(
                              wizardOcrConfig,
                              'mixed',
                            );
                          });
                        },
                      ),
                      ChoiceChip(
                        label: const Text('Cahier des charges'),
                        selected:
                            ((wizardOcrConfig['documentTypeHint'] as String?) ??
                                    'mixed') ==
                                'products_with_specs',
                        onSelected: (_) {
                          setStateDialog(() {
                            wizardOcrConfig = _applyStandardAnalysisType(
                              wizardOcrConfig,
                              'products_with_specs',
                            );
                          });
                        },
                      ),
                      ChoiceChip(
                        label: const Text('Offre simple'),
                        selected:
                            ((wizardOcrConfig['documentTypeHint'] as String?) ??
                                    'mixed') ==
                                'simple_list',
                        onSelected: (_) {
                          setStateDialog(() {
                            wizardOcrConfig = _applyStandardAnalysisType(
                              wizardOcrConfig,
                              'simple_list',
                            );
                          });
                        },
                      ),
                    ],
                  ),
                ] else ...[
                  const SizedBox(height: 12),
                  Container(
                    width: double.infinity,
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: const Color(0xFFF4F6F8),
                      borderRadius: BorderRadius.circular(10),
                      border: Border.all(color: const Color(0xFFD7DEE5)),
                    ),
                    child: const Text(
                      'Profil actif: Liste produits stricte',
                    ),
                  ),
                ],
                const SizedBox(height: 12),
                TextField(
                  controller: establishmentNameController,
                  decoration: const InputDecoration(
                    labelText: 'Nom etablissement',
                  ),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: lotReferenceController,
                  decoration: const InputDecoration(labelText: 'Lot'),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: establishmentAddressController,
                  decoration: const InputDecoration(labelText: 'Adresse'),
                  maxLines: 2,
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: establishmentPhoneController,
                  decoration: const InputDecoration(labelText: 'Telephone'),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: pharmacyServiceController,
                  decoration: const InputDecoration(
                    labelText: 'Service pharmacie',
                  ),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: marketOfficeServiceController,
                  decoration: const InputDecoration(
                    labelText: 'Service bureau de marche',
                  ),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: establishmentTypeController,
                  decoration: const InputDecoration(
                    labelText: 'Type d\'etablissement',
                  ),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: depositDateController,
                  decoration: const InputDecoration(
                    labelText: 'Date de depot',
                    hintText: 'YYYY-MM-DD',
                  ),
                ),
                const SizedBox(height: 10),
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  value: processingInProgress,
                  onChanged: (value) {
                    setStateDialog(() {
                      processingInProgress = value ?? false;
                    });
                  },
                  title: const Text('En cours de traitement'),
                ),
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  value: processingValidated,
                  onChanged: (value) {
                    setStateDialog(() {
                      processingValidated = value ?? false;
                    });
                  },
                  title: const Text('Valide'),
                ),
              ],
            );
          } else if (currentStep == 1) {
            stepContent = Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'Etape 2/5 - Email et details',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 12),
                if (_templates.isNotEmpty)
                  DropdownButtonFormField<int>(
                    value: selectedTemplateId,
                    decoration: const InputDecoration(labelText: 'Template'),
                    items: _templates
                        .map(
                          (t) => DropdownMenuItem<int>(
                            value: t['id'] as int,
                            child: Text(t['name'] as String),
                          ),
                        )
                        .toList(),
                    onChanged: (value) {
                      setStateDialog(() => selectedTemplateId = value);
                      final template =
                          value == null ? null : templatesById[value];
                      if (template != null) {
                        subjectController.text = template['subject'] ?? '';
                        messageController.text = template['body_html'] ?? '';
                      }
                    },
                  ),
                if (_templates.isNotEmpty) const SizedBox(height: 12),
                TextField(
                  controller: titleController,
                  decoration: const InputDecoration(labelText: 'Campaign name'),
                  onChanged: (_) => setStateDialog(() {}),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: subjectController,
                  decoration: const InputDecoration(labelText: 'Email subject'),
                  onChanged: (_) => setStateDialog(() {}),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: messageController,
                  decoration: const InputDecoration(
                    labelText: 'Email message (HTML)',
                  ),
                  maxLines: 10,
                  onChanged: (_) => setStateDialog(() {}),
                ),
                const SizedBox(height: 10),
                TextField(
                  controller: priorityController,
                  decoration: const InputDecoration(labelText: 'Priority'),
                  keyboardType: TextInputType.number,
                ),
              ],
            );
          } else if (currentStep == 2) {
            stepContent = Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'Etape 3/5 - Destinataires',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 8),
                Text(
                  'Selectionne les fournisseurs ou des groupes entiers.',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
                const SizedBox(height: 12),
                TextField(
                  decoration: const InputDecoration(
                    labelText: 'Rechercher un fournisseur',
                    prefixIcon: Icon(Icons.search),
                  ),
                  onChanged: (value) => setStateDialog(() {
                    recipientSearch = value;
                  }),
                ),
                const SizedBox(height: 12),
                const Text(
                  'Groupes',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 8),
                ..._groups.map((group) {
                  final groupId = group['id'] as int;
                  final members = groupMembers[groupId] ?? <int>{};
                  return CheckboxListTile(
                    tristate: true,
                    contentPadding: EdgeInsets.zero,
                    value: groupValue(members),
                    onChanged: members.isEmpty
                        ? null
                        : (_) {
                            setStateDialog(() {
                              toggleGroupSelection(members);
                            });
                          },
                    title: Text((group['name'] ?? '-').toString()),
                    subtitle: Text('Membres: ${members.length}'),
                  );
                }),
                const Divider(height: 24),
                const Text(
                  'Fournisseurs',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 8),
                SizedBox(
                  height: 260,
                  child: ListView(
                    children: filteredSuppliers.map((supplier) {
                      final supplierId = supplier['id'] as int;
                      final email = (supplier['email'] ?? '').toString().trim();
                      return CheckboxListTile(
                        contentPadding: EdgeInsets.zero,
                        value: selectedSupplierIds.contains(supplierId),
                        onChanged: (value) {
                          setStateDialog(() {
                            if (value == true) {
                              selectedSupplierIds.add(supplierId);
                            } else {
                              selectedSupplierIds.remove(supplierId);
                            }
                          });
                        },
                        title: Text((supplier['name'] ?? '-').toString()),
                        subtitle: email.isEmpty ? null : Text(email),
                      );
                    }).toList(),
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  '${selectedSupplierIds.length} fournisseur(s) selectionne(s)',
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
              ],
            );
          } else if (currentStep == 3) {
            final wizardOcrSummary = _documentConfigSummaryForMode(
              wizardOcrConfig,
              processingMode,
            );
            stepContent = Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'Etape 4/5 - Pieces jointes',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 8),
                Text(
                  'Ajoute des PDFs si la campagne doit partir avec un cahier des charges ou une liste demandee.',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    OutlinedButton.icon(
                      onPressed: () async {
                        final picked = await FilePicker.platform.pickFiles(
                          type: FileType.custom,
                          allowedExtensions: const ['pdf'],
                          allowMultiple: true,
                        );
                        if (picked == null || picked.files.isEmpty) return;
                        setStateDialog(() {
                          for (final file in picked.files) {
                            final path = (file.path ?? '').trim();
                            if (path.isNotEmpty &&
                                !attachmentPaths.contains(path)) {
                              attachmentPaths.add(path);
                            }
                          }
                        });
                      },
                      icon: const Icon(Icons.attach_file_outlined),
                      label: const Text('Ajouter PDF'),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                if (processingMode == 'standard') ...[
                  const Text(
                    'Type d\'analyse',
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      ChoiceChip(
                        label: const Text('Mixte'),
                        selected:
                            ((wizardOcrConfig['documentTypeHint'] as String?) ??
                                    'mixed') ==
                                'mixed',
                        onSelected: (_) {
                          setStateDialog(() {
                            wizardOcrConfig = _applyStandardAnalysisType(
                              wizardOcrConfig,
                              'mixed',
                            );
                          });
                        },
                      ),
                      ChoiceChip(
                        label: const Text('Cahier des charges'),
                        selected:
                            ((wizardOcrConfig['documentTypeHint'] as String?) ??
                                    'mixed') ==
                                'products_with_specs',
                        onSelected: (_) {
                          setStateDialog(() {
                            wizardOcrConfig = _applyStandardAnalysisType(
                              wizardOcrConfig,
                              'products_with_specs',
                            );
                          });
                        },
                      ),
                      ChoiceChip(
                        label: const Text('Offre simple'),
                        selected:
                            ((wizardOcrConfig['documentTypeHint'] as String?) ??
                                    'mixed') ==
                                'simple_list',
                        onSelected: (_) {
                          setStateDialog(() {
                            wizardOcrConfig = _applyStandardAnalysisType(
                              wizardOcrConfig,
                              'simple_list',
                            );
                          });
                        },
                      ),
                    ],
                  ),
                ] else ...[
                  Container(
                    width: double.infinity,
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: const Color(0xFFF4F6F8),
                      borderRadius: BorderRadius.circular(10),
                      border: Border.all(color: const Color(0xFFD7DEE5)),
                    ),
                    child: const Text('Profil: Liste produits stricte'),
                  ),
                ],
                const SizedBox(height: 12),
                const Text(
                  'Structure dominante',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    ChoiceChip(
                      label: const Text('Auto'),
                      selected: ((wizardOcrConfig['layoutHint'] as String?) ??
                              'auto') ==
                          'auto',
                      onSelected: (_) {
                        setStateDialog(() {
                          wizardOcrConfig = {
                            ...wizardOcrConfig,
                            'layoutHint': 'auto',
                          };
                        });
                      },
                    ),
                    ChoiceChip(
                      label: const Text('Tableau/Liste'),
                      selected: ((wizardOcrConfig['layoutHint'] as String?) ??
                              'auto') ==
                          'table',
                      onSelected: (_) {
                        setStateDialog(() {
                          wizardOcrConfig = {
                            ...wizardOcrConfig,
                            'layoutHint': 'table',
                          };
                        });
                      },
                    ),
                    ChoiceChip(
                      label: const Text('Paragraphe'),
                      selected: ((wizardOcrConfig['layoutHint'] as String?) ??
                              'auto') ==
                          'paragraph',
                      onSelected: (_) {
                        setStateDialog(() {
                          wizardOcrConfig = {
                            ...wizardOcrConfig,
                            'layoutHint': 'paragraph',
                          };
                        });
                      },
                    ),
                    ChoiceChip(
                      label: const Text('Mixte'),
                      selected: ((wizardOcrConfig['layoutHint'] as String?) ??
                              'auto') ==
                          'mixed',
                      onSelected: (_) {
                        setStateDialog(() {
                          wizardOcrConfig = {
                            ...wizardOcrConfig,
                            'layoutHint': 'mixed',
                          };
                        });
                      },
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Text(
                  wizardOcrSummary,
                  style: const TextStyle(
                    color: Colors.black54,
                    fontSize: 12,
                  ),
                ),
                const SizedBox(height: 12),
                if (attachmentPaths.isEmpty)
                  const Text(
                    'Aucun PDF ajoute. Cette etape est optionnelle.',
                    style: TextStyle(color: Colors.black54),
                  )
                else
                  SizedBox(
                    height: 340,
                    child: ListView.builder(
                      itemCount: attachmentPaths.length,
                      itemBuilder: (context, index) {
                        final path = attachmentPaths[index];
                        final name = path.split(Platform.pathSeparator).last;
                        return ListTile(
                          dense: true,
                          leading: const Icon(
                            Icons.picture_as_pdf,
                            color: Colors.red,
                          ),
                          title: Text(name),
                          subtitle: Text(path,
                              maxLines: 1, overflow: TextOverflow.ellipsis),
                          trailing: IconButton(
                            onPressed: () {
                              setStateDialog(() {
                                attachmentPaths.removeAt(index);
                              });
                            },
                            icon: const Icon(Icons.delete_outline),
                          ),
                        );
                      },
                    ),
                  ),
              ],
            );
          } else {
            final selectedSuppliers = _suppliers.where((raw) {
              final id = raw['id'];
              return id is int && selectedSupplierIds.contains(id);
            }).toList();
            stepContent = Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'Etape 5/5 - Verification avant creation',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 12),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Etablissement: ${establishmentNameController.text.trim().isEmpty ? '-' : establishmentNameController.text.trim()}',
                        ),
                        const SizedBox(height: 6),
                        Text(
                          'Lot: ${lotReferenceController.text.trim().isEmpty ? '-' : lotReferenceController.text.trim()}',
                        ),
                        const SizedBox(height: 6),
                        Text('Titre: ${titleController.text.trim()}'),
                        const SizedBox(height: 6),
                        Text(
                          'Mode: ${processingMode == 'strict' ? 'Strict' : 'Standard'}',
                        ),
                        if (processingMode == 'standard') ...[
                          const SizedBox(height: 6),
                          Text(
                            'Type d\'analyse: ${_documentHintLabel(((wizardOcrConfig['documentTypeHint'] as String?) ?? 'mixed'))}',
                          ),
                        ],
                        const SizedBox(height: 6),
                        Text(
                          'Structure dominante: ${_layoutHintLabel(((wizardOcrConfig['layoutHint'] as String?) ?? 'auto'))}',
                        ),
                        const SizedBox(height: 6),
                        Text('Sujet: ${subjectController.text.trim()}'),
                        const SizedBox(height: 6),
                        Text(
                          'Date depot: ${depositDateController.text.trim().isEmpty ? '-' : depositDateController.text.trim()}',
                        ),
                        const SizedBox(height: 6),
                        Text('Destinataires: ${selectedSuppliers.length}'),
                        const SizedBox(height: 6),
                        Text('PDFs: ${attachmentPaths.length}'),
                        const SizedBox(height: 6),
                        const Text('Analyse auto: non'),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                const Text(
                  'Checklist',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 8),
                _buildWizardCheckTile(
                  'Informations campagne renseignees',
                  establishmentNameController.text.trim().isNotEmpty ||
                      lotReferenceController.text.trim().isNotEmpty ||
                      establishmentAddressController.text.trim().isNotEmpty ||
                      establishmentPhoneController.text.trim().isNotEmpty ||
                      pharmacyServiceController.text.trim().isNotEmpty ||
                      marketOfficeServiceController.text.trim().isNotEmpty ||
                      establishmentTypeController.text.trim().isNotEmpty ||
                      depositDateController.text.trim().isNotEmpty,
                  note: 'Au moins une information administrative',
                ),
                _buildWizardCheckTile(
                  'Nom de campagne rempli',
                  titleController.text.trim().isNotEmpty,
                ),
                _buildWizardCheckTile(
                  'Sujet email rempli',
                  subjectController.text.trim().isNotEmpty,
                ),
                _buildWizardCheckTile(
                  'Message email rempli',
                  messageController.text.trim().isNotEmpty,
                ),
                _buildWizardCheckTile(
                  'Au moins un destinataire selectionne',
                  selectedSuppliers.isNotEmpty,
                ),
                _buildWizardCheckTile(
                  'Produits geres apres creation',
                  true,
                  note: 'Via OCR ou edition de campagne',
                ),
                _buildWizardCheckTile(
                  'Pieces jointes optionnelles',
                  true,
                  note: attachmentPaths.isEmpty
                      ? 'Aucun PDF ajoute'
                      : '${attachmentPaths.length} PDF(s) seront televerses',
                ),
              ],
            );
          }

          return AlertDialog(
            title: const Text('Assistant de creation campagne'),
            content: SizedBox(
              width: 980,
              height: 680,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  LinearProgressIndicator(
                    value: (currentStep + 1) / 5,
                    minHeight: 6,
                  ),
                  const SizedBox(height: 12),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: List.generate(5, (index) {
                      final labels = [
                        'Infos',
                        'Details',
                        'Destinataires',
                        'PDF',
                        'Verification',
                      ];
                      return ChoiceChip(
                        label: Text('${index + 1}. ${labels[index]}'),
                        selected: currentStep == index,
                        onSelected: (selected) {
                          if (!selected) return;
                          if (index <= currentStep ||
                              (index == currentStep + 1 &&
                                  canContinueFrom(currentStep))) {
                            setStateDialog(() => currentStep = index);
                          }
                        },
                      );
                    }),
                  ),
                  const SizedBox(height: 16),
                  Expanded(
                    child: SingleChildScrollView(
                      child: stepContent,
                    ),
                  ),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('Annuler'),
              ),
              if (currentStep > 0)
                OutlinedButton(
                  onPressed: () => setStateDialog(() => currentStep -= 1),
                  child: const Text('Precedent'),
                ),
              if (currentStep < 4)
                ElevatedButton(
                  onPressed: canContinueFrom(currentStep)
                      ? () => setStateDialog(() => currentStep += 1)
                      : null,
                  child: const Text('Suivant'),
                )
              else
                ElevatedButton(
                  onPressed: (selectedSupplierIds.isNotEmpty &&
                          titleController.text.trim().isNotEmpty &&
                          subjectController.text.trim().isNotEmpty &&
                          messageController.text.trim().isNotEmpty)
                      ? () => Navigator.pop(context, true)
                      : null,
                  child: const Text('Creer la campagne'),
                ),
            ],
          );
        },
      ),
    );

    if (result == true) {
      final rootNavigator = Navigator.of(context, rootNavigator: true);
      final messenger = ScaffoldMessenger.of(context);
      final progressMessage =
          ValueNotifier<String>('Creation de la campagne...');
      var loadingVisible = false;
      if (mounted) {
        loadingVisible = true;
        showDialog<void>(
          context: context,
          barrierDismissible: false,
          builder: (context) => AlertDialog(
            content: SizedBox(
              width: 360,
              child: ValueListenableBuilder<String>(
                valueListenable: progressMessage,
                builder: (context, message, _) => Row(
                  children: [
                    const SizedBox(
                      width: 24,
                      height: 24,
                      child: CircularProgressIndicator(strokeWidth: 2.5),
                    ),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          const Text(
                            'Creation guidee en cours',
                            style: TextStyle(fontWeight: FontWeight.w700),
                          ),
                          const SizedBox(height: 4),
                          Text(message),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
        await WidgetsBinding.instance.endOfFrame;
      }

      Map<String, dynamic>? createdCampaign;
      var campaignId = 0;
      var campaignCreated = false;
      var itemsSaved = false;
      var recipientsSaved = false;
      var attachmentsUploaded = false;
      const ocrRan = true;
      var previewOk = false;
      int createdRecipientsCount = 0;
      int createdAttachmentsCount = 0;
      final issues = <String>[];

      try {
        progressMessage.value = 'Enregistrement de la campagne...';
        final title = titleController.text.trim().isEmpty
            ? suggestedTitle
            : titleController.text.trim();
        createdCampaign = await CampaignsApi.create({
          'title': title,
          'subject': subjectController.text.trim(),
          'message_html': messageController.text.trim(),
          'priority': int.tryParse(priorityController.text.trim()) ?? 0,
          'processing_mode': processingMode,
          'establishment_name': establishmentNameController.text.trim(),
          'lot_reference': lotReferenceController.text.trim(),
          'establishment_address': establishmentAddressController.text.trim(),
          'establishment_phone': establishmentPhoneController.text.trim(),
          'pharmacy_service': pharmacyServiceController.text.trim(),
          'market_office_service': marketOfficeServiceController.text.trim(),
          'establishment_type': establishmentTypeController.text.trim(),
          'deposit_date':
              _parseDateOnly(depositDateController.text)?.toIso8601String(),
          'processing_in_progress': processingInProgress,
          'processing_validated': processingValidated,
        });
        campaignId = createdCampaign['id'] as int;
        if (processingMode == 'strict') {
          final createdMode = (createdCampaign['processing_mode'] ?? '')
              .toString()
              .trim()
              .toLowerCase();
          if (createdMode != 'strict') {
            progressMessage.value = 'Verification du mode strict...';
            createdCampaign = await CampaignsApi.update(
              campaignId,
              {'processing_mode': 'strict'},
            );
          }
        }
        campaignCreated = true;

        progressMessage.value = 'Actualisation de la campagne...';
        await _load();
        await _selectCampaign(createdCampaign);
        itemsSaved = true;

        try {
          progressMessage.value = 'Enregistrement des destinataires...';
          await CampaignsApi.setRecipients(
            campaignId,
            const [],
            selectedSupplierIds.toList(),
          );
          final createdRecipients = await CampaignsApi.recipients(campaignId);
          createdRecipientsCount = createdRecipients.length;
          recipientsSaved =
              createdRecipientsCount == selectedSupplierIds.length;
          if (!recipientsSaved) {
            issues.add(
              'Destinataires enregistres partiellement ($createdRecipientsCount/${selectedSupplierIds.length})',
            );
          }
        } catch (err) {
          issues.add('Destinataires: ${ApiClient.formatError(err)}');
        }

        try {
          progressMessage.value = 'Televersement des pieces jointes...';
          for (var i = 0; i < attachmentPaths.length; i++) {
            final path = attachmentPaths[i];
            final fileName = path.split(RegExp(r'[\\/]')).last;
            progressMessage.value =
                'Televersement des pieces jointes... ${i + 1}/${attachmentPaths.length} ($fileName)';
            try {
              await CampaignsApi.uploadAttachment(campaignId, path);
              createdAttachmentsCount += 1;
            } catch (err) {
              issues.add(
                'Piece jointe "$fileName": ${ApiClient.formatError(err)}',
              );
            }
          }
          final createdAttachments = await CampaignsApi.attachments(campaignId);
          createdAttachmentsCount = createdAttachments.length;
          attachmentsUploaded =
              createdAttachmentsCount >= attachmentPaths.length;
          if (!attachmentsUploaded) {
            issues.add(
              'Pieces jointes televersees partiellement ($createdAttachmentsCount/${attachmentPaths.length})',
            );
          }
        } catch (err) {
          issues.add('Pieces jointes: ${ApiClient.formatError(err)}');
        }

        try {
          progressMessage.value = 'Generation de la previsualisation email...';
          final html = await CampaignsApi.previewHtml(campaignId);
          previewOk = html.trim().isNotEmpty;
          if (!previewOk) {
            issues.add('Preview email vide');
          }
        } catch (err) {
          issues.add('Preview email: ${ApiClient.formatError(err)}');
        }
      } catch (err) {
        issues.add('Creation campagne: ${ApiClient.formatError(err)}');
      }

      if (mounted && loadingVisible) {
        rootNavigator.pop();
      }
      progressMessage.dispose();

      if (mounted && campaignCreated && createdCampaign != null) {
        await _load();
        await _selectCampaign(createdCampaign);
      }

      if (!mounted) {
        // fall through to dispose below
      } else if (campaignCreated) {
        messenger.showSnackBar(
          const SnackBar(content: Text('Creation campagne terminee')),
        );
        await showDialog<void>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('Creation guidee terminee'),
            content: SizedBox(
              width: 560,
              child: SingleChildScrollView(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    _buildWizardCheckTile(
                      'Campagne creee',
                      campaignCreated,
                      note: 'ID $campaignId',
                    ),
                    _buildWizardCheckTile(
                      'Produits enregistres',
                      itemsSaved,
                      note: 'Etape retiree du wizard',
                    ),
                    _buildWizardCheckTile(
                      'Destinataires enregistres',
                      recipientsSaved,
                      note:
                          '$createdRecipientsCount/${selectedSupplierIds.length}',
                    ),
                    _buildWizardCheckTile(
                      'Pieces jointes televersees',
                      attachmentsUploaded || attachmentPaths.isEmpty,
                      note:
                          '$createdAttachmentsCount/${attachmentPaths.length}',
                    ),
                    _buildWizardCheckTile(
                      'OCR execute',
                      ocrRan,
                      note: 'A lancer manuellement depuis la campagne',
                    ),
                    _buildWizardCheckTile(
                      'Preview email OK',
                      previewOk,
                      note: previewOk
                          ? 'Rendu email disponible'
                          : 'Preview a verifier',
                    ),
                    if (issues.isNotEmpty) ...[
                      const SizedBox(height: 12),
                      const Text(
                        'Points a verifier',
                        style: TextStyle(fontWeight: FontWeight.w700),
                      ),
                      const SizedBox(height: 8),
                      ...issues.map(
                        (issue) => Padding(
                          padding: const EdgeInsets.only(bottom: 6),
                          child: Text(issue),
                        ),
                      ),
                    ],
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
          ),
        );
      } else if (mounted && issues.isNotEmpty) {
        _showError(issues.join(' | '));
      }
    }

    titleController.dispose();
    establishmentNameController.dispose();
    lotReferenceController.dispose();
    establishmentAddressController.dispose();
    establishmentPhoneController.dispose();
    pharmacyServiceController.dispose();
    marketOfficeServiceController.dispose();
    establishmentTypeController.dispose();
    depositDateController.dispose();
    subjectController.dispose();
    messageController.dispose();
    priorityController.dispose();
  }

  Widget _buildWizardCheckTile(String label, bool ok, {String? note}) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            ok ? Icons.check_circle_outline : Icons.error_outline,
            size: 20,
            color: ok ? Colors.green : Colors.orange.shade700,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  label,
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
                if (note != null && note.trim().isNotEmpty)
                  Text(
                    note,
                    style: const TextStyle(fontSize: 12, color: Colors.black54),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _manageItems() async {
    if (_selectedCampaign == null) return;
    final selected = <int>{};
    final qtyControllers = <int, TextEditingController>{};
    final descControllers = <int, TextEditingController>{};
    final charsControllers = <int, TextEditingController>{};

    for (final item in _campaignItems) {
      final productId = item['product_id'] as int;
      selected.add(productId);
      qtyControllers[productId] = TextEditingController(
        text: item['requested_qty']?.toString() ?? '',
      );
      final requestedDesc =
          (item['requested_description']?.toString() ?? '').trim();
      final productDesc =
          (item['product_description']?.toString() ?? '').trim();
      descControllers[productId] = TextEditingController(
        text: requestedDesc.isNotEmpty ? requestedDesc : productDesc,
      );
      charsControllers[productId] = TextEditingController(
        text: _characteristicsToEditorText(
          item['requested_characteristics'] ?? item['product_characteristics'],
        ),
      );
    }

    final result = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setStateDialog) => AlertDialog(
          title: const Text('Campaign Items'),
          content: SizedBox(
            width: 760,
            height: 560,
            child: ListView(
              children: _products.map((product) {
                final productId = product['id'] as int;
                final controller = qtyControllers.putIfAbsent(
                    productId, () => TextEditingController());
                final descController = descControllers.putIfAbsent(
                  productId,
                  () => TextEditingController(
                    text: (product['description']?.toString() ?? '').trim(),
                  ),
                );
                final charsController = charsControllers.putIfAbsent(
                  productId,
                  () => TextEditingController(
                    text: _characteristicsToEditorText(
                        product['characteristics']),
                  ),
                );

                return Card(
                  margin: const EdgeInsets.symmetric(vertical: 6),
                  child: Padding(
                    padding: const EdgeInsets.all(10),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        CheckboxListTile(
                          contentPadding: EdgeInsets.zero,
                          value: selected.contains(productId),
                          onChanged: (value) {
                            if (value == true) {
                              selected.add(productId);
                            } else {
                              selected.remove(productId);
                            }
                            setStateDialog(() {});
                          },
                          title: Text(product['name'] as String),
                          subtitle: Text('Unit: ${product['unit'] ?? ''}'),
                        ),
                        if (selected.contains(productId)) ...[
                          const SizedBox(height: 6),
                          SizedBox(
                            width: 160,
                            child: TextField(
                              controller: controller,
                              decoration: const InputDecoration(
                                  labelText: 'Requested qty'),
                              keyboardType: TextInputType.number,
                            ),
                          ),
                          const SizedBox(height: 8),
                          TextField(
                            controller: descController,
                            decoration: const InputDecoration(
                              labelText: 'Requested description',
                            ),
                            maxLines: 2,
                          ),
                          const SizedBox(height: 8),
                          TextField(
                            controller: charsController,
                            decoration: const InputDecoration(
                              labelText:
                                  'Requested characteristics (one per line: key: value)',
                            ),
                            maxLines: 5,
                          ),
                        ],
                      ],
                    ),
                  ),
                );
              }).toList(),
            ),
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('Cancel')),
            ElevatedButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('Save')),
          ],
        ),
      ),
    );

    if (result == true) {
      final items = selected.map((productId) {
        final qtyText = qtyControllers[productId]?.text.trim();
        final descText = descControllers[productId]?.text.trim() ?? '';
        final charsText = charsControllers[productId]?.text.trim() ?? '';
        final existing =
            _campaignItems.whereType<Map<String, dynamic>>().firstWhere(
                  (e) => e['product_id'] == productId,
                  orElse: () => <String, dynamic>{},
                );
        return {
          'product_id': productId,
          'requested_qty': qtyText == null || qtyText.isEmpty
              ? null
              : double.tryParse(qtyText),
          'requested_description': descText.isEmpty ? null : descText,
          'requested_characteristics':
              _characteristicsFromEditorText(charsText),
          'requested_embedding_score': existing['requested_embedding_score'],
        };
      }).toList();
      try {
        await CampaignsApi.setItems(_selectedCampaign!['id'] as int, items);
        await _loadCampaignItems();
        setState(() {});
      } catch (err) {
        if (mounted) {
          _showError(err);
        }
      }
    }

    for (final c in qtyControllers.values) {
      c.dispose();
    }
    for (final c in descControllers.values) {
      c.dispose();
    }
    for (final c in charsControllers.values) {
      c.dispose();
    }
  }

  String _characteristicsToEditorText(dynamic raw) {
    final map = _characteristicsFromDynamic(raw);
    if (map.isEmpty) return '';
    final entries = map.entries.toList()
      ..sort((a, b) => a.key.compareTo(b.key));
    final lines = <String>[];
    for (final entry in entries) {
      for (final value in entry.value) {
        final key = entry.key.trim();
        final val = value.trim();
        if (key.isEmpty || val.isEmpty) continue;
        lines.add('$key: $val');
      }
    }
    return lines.join('\n');
  }

  Map<String, dynamic>? _characteristicsFromEditorText(String raw) {
    if (raw.trim().isEmpty) return null;
    final out = <String, List<Map<String, String>>>{};
    final lines = raw.split(RegExp(r'[\r\n]+'));
    for (final line in lines) {
      final text = line.trim();
      if (text.isEmpty) continue;
      final idx = text.indexOf(':');
      if (idx <= 0 || idx >= text.length - 1) continue;
      final key = text.substring(0, idx).trim();
      final value = text.substring(idx + 1).trim();
      if (key.isEmpty || value.isEmpty) continue;
      final bucket = out.putIfAbsent(key, () => <Map<String, String>>[]);
      final duplicate = bucket.any(
        (row) => (row['value'] ?? '').toLowerCase() == value.toLowerCase(),
      );
      if (!duplicate) {
        bucket.add({'value': value});
      }
    }
    if (out.isEmpty) return null;
    return out;
  }

  Future<void> _manageRecipients() async {
    if (_selectedCampaign == null) return;
    final selectedSupplierIds = <int>{};
    final groupMembers = <int, Set<int>>{};

    try {
      final recipients =
          await CampaignsApi.recipients(_selectedCampaign!['id'] as int);
      selectedSupplierIds
          .addAll(recipients.map<int>((r) => r['supplier_id'] as int));

      final memberEntries = await Future.wait(
        _groups.map((group) async {
          final groupId = group['id'] as int;
          final members = await GroupsApi.listMembers(groupId);
          return MapEntry(
            groupId,
            members.map<int>((m) => m['supplier_id'] as int).toSet(),
          );
        }),
      );
      for (final entry in memberEntries) {
        groupMembers[entry.key] = entry.value;
      }
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
      return;
    }

    if (!mounted) return;
    bool? groupValue(Set<int> members) {
      if (members.isEmpty) return false;
      final selectedCount = members.where(selectedSupplierIds.contains).length;
      if (selectedCount == 0) return false;
      if (selectedCount == members.length) return true;
      return null;
    }

    void toggleGroupSelection(Set<int> members) {
      if (members.isEmpty) return;
      final current = groupValue(members);
      if (current == true) {
        selectedSupplierIds.removeAll(members);
      } else {
        selectedSupplierIds.addAll(members);
      }
    }

    final result = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setStateDialog) => AlertDialog(
          title: const Text('Recipients'),
          content: SizedBox(
            width: 520,
            height: 440,
            child: ListView(
              children: [
                const Text('Groups',
                    style: TextStyle(fontWeight: FontWeight.bold)),
                ..._groups.map((g) {
                  final groupId = g['id'] as int;
                  final members = groupMembers[groupId] ?? <int>{};
                  return CheckboxListTile(
                    tristate: true,
                    value: groupValue(members),
                    onChanged: members.isEmpty
                        ? null
                        : (_) {
                            toggleGroupSelection(members);
                            setStateDialog(() {});
                          },
                    title: Text(g['name'] as String),
                    subtitle: Text('Members: ${members.length}'),
                  );
                }),
                const Divider(),
                const Text('Suppliers',
                    style: TextStyle(fontWeight: FontWeight.bold)),
                ..._suppliers.map((s) {
                  final supplierId = s['id'] as int;
                  return CheckboxListTile(
                    value: selectedSupplierIds.contains(supplierId),
                    onChanged: (value) {
                      if (value == true) {
                        selectedSupplierIds.add(supplierId);
                      } else {
                        selectedSupplierIds.remove(supplierId);
                      }
                      setStateDialog(() {});
                    },
                    title: Text(s['name'] as String),
                  );
                }),
              ],
            ),
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('Cancel')),
            ElevatedButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('Save')),
          ],
        ),
      ),
    );

    if (result == true) {
      try {
        await CampaignsApi.setRecipients(
          _selectedCampaign!['id'] as int,
          const [],
          selectedSupplierIds.toList(),
        );
        if (!mounted) return;
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('Recipients saved')));
      } catch (err) {
        if (mounted) {
          _showError(err);
        }
      }
    }
  }

  Future<void> _previewEmail() async {
    if (_selectedCampaign == null) return;
    try {
      final html =
          await CampaignsApi.previewHtml(_selectedCampaign!['id'] as int);
      if (!mounted) return;
      showDialog(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Email Preview'),
          content:
              SizedBox(width: 600, height: 500, child: HtmlPreview(html: html)),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('Close')),
          ],
        ),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  Future<void> _addPdfAttachments() async {
    if (_selectedCampaign == null) return;
    final picked = await FilePicker.platform.pickFiles(
      type: FileType.custom,
      allowedExtensions: const ['pdf'],
      allowMultiple: true,
    );
    if (picked == null || picked.files.isEmpty) return;
    final campaignId = _selectedCampaign!['id'] as int;
    final files = picked.files.where((f) => (f.path ?? '').isNotEmpty).toList();
    if (files.isEmpty) return;

    setState(() {
      _ocrAnalyzing = true;
      _ocrProgress = 0.0;
      _ocrStartedAt = DateTime.now();
      _ocrStatus = 'Televersement PDFs... 0/${files.length}';
    });

    var uploaded = 0;
    try {
      for (var i = 0; i < files.length; i++) {
        final file = files[i];
        final path = file.path!;
        if (mounted) {
          setState(() {
            _ocrStatus =
                'Televersement PDFs... ${i + 1}/${files.length} (${file.name})';
            _ocrProgress = i / files.length;
          });
        }

        try {
          await CampaignsApi.uploadAttachment(campaignId, path);
          uploaded += 1;
        } catch (err) {
          if (mounted) {
            _showError(err);
          }
        }

        if (mounted) {
          setState(() {
            _ocrProgress = (i + 1) / files.length;
          });
        }
      }
      await _loadAttachments();
    } finally {
      if (mounted) {
        setState(() {
          _ocrAnalyzing = false;
          _ocrProgress = 0.0;
          _ocrStatus = '';
          _ocrStartedAt = null;
        });
      }
    }

    if (!mounted) return;
    setState(() {});
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text('$uploaded PDF ajoute(s) a la campagne')),
    );
  }

  Future<void> _deleteAttachment(int attachmentId) async {
    if (_selectedCampaign == null) return;
    final campaignId = _selectedCampaign!['id'] as int;
    try {
      await CampaignsApi.deleteAttachment(campaignId, attachmentId);
      await _loadAttachments();
      if (!mounted) return;
      setState(() {});
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  int? _parsePositiveInt(String raw) {
    final value = int.tryParse(raw.trim());
    if (value == null || value <= 0) return null;
    return value;
  }

  String _mapUiDocumentHintToBackend(String uiHint, String layoutHint) {
    final hint = uiHint.trim().toLowerCase();
    if (hint == 'simple_list') {
      return 'product_list';
    }
    if (hint == 'strict_produit') {
      return 'strict_produit';
    }
    if (hint == 'products_with_specs') {
      return 'requirement_sheet';
    }
    return 'mixed';
  }

  String _documentHintLabel(String hint) {
    switch (hint.trim().toLowerCase()) {
      case 'simple_list':
        return 'Offre simple';
      case 'products_with_specs':
        return 'Cahier des charges';
      case 'strict_produit':
        return 'Liste produits stricte';
      case 'mixed':
      default:
        return 'Mixte';
    }
  }

  String _layoutHintLabel(String hint) {
    switch (hint.trim().toLowerCase()) {
      case 'table':
        return 'Tableau';
      case 'paragraph':
        return 'Paragraphe';
      case 'mixed':
        return 'Mixte';
      case 'auto':
      default:
        return 'Auto';
    }
  }

  Map<String, dynamic> _applyProcessingModeDefaults(
    Map<String, dynamic> config,
    String processingMode,
  ) {
    final normalized = _normalizedOcrExtractionConfig(config);
    if (processingMode == 'strict') {
      return {
        ...normalized,
        'documentTypeHint': 'strict_produit',
        'layoutHint': 'auto',
      };
    }
    final currentHint =
        (normalized['documentTypeHint'] as String?)?.trim().toLowerCase() ??
            'mixed';
    if (currentHint == 'strict_produit') {
      return {
        ...normalized,
        'documentTypeHint': 'mixed',
      };
    }
    return normalized;
  }

  Map<String, dynamic> _applyStandardAnalysisType(
    Map<String, dynamic> config,
    String documentTypeHint,
  ) {
    final normalized = _normalizedOcrExtractionConfig(config);
    final hint = documentTypeHint.trim().toLowerCase();
    final layout = switch (hint) {
      'simple_list' => 'table',
      'products_with_specs' => 'paragraph',
      _ => 'auto',
    };
    return {
      ...normalized,
      'documentTypeHint': hint,
      'layoutHint': layout,
    };
  }

  Map<String, dynamic> _normalizedOcrExtractionConfig(
      [Map<String, dynamic>? raw]) {
    final source = raw ?? const <String, dynamic>{};
    final expectedRaw = source['expectedProductCounts'];
    final expectedMap = <String, int>{};
    if (expectedRaw is Map) {
      expectedRaw.forEach((k, v) {
        final n = int.tryParse((v ?? '').toString());
        if (n != null && n > 0) {
          expectedMap[k.toString()] = n;
        }
      });
    }

    final docHint =
        (source['documentTypeHint']?.toString().trim().toLowerCase() ??
            'mixed');
    final normalizedDocHint = {
      'simple_list',
      'products_with_specs',
      'mixed',
      'strict_produit'
    }.contains(docHint)
        ? docHint
        : 'mixed';

    final layoutHint =
        (source['layoutHint']?.toString().trim().toLowerCase() ?? 'auto');
    final normalizedLayoutHint =
        {'auto', 'table', 'paragraph', 'mixed'}.contains(layoutHint)
            ? layoutHint
            : 'auto';

    final expectedGlobal =
        _parsePositiveInt((source['expectedProductCount'] ?? '').toString());

    return {
      'useConstraint': true,
      'expectedProductCount': expectedGlobal,
      'expectedProductCounts': expectedMap,
      'strictExpectedCount': true,
      'strictCountMode': 'strict',
      'documentTypeHint': normalizedDocHint,
      'layoutHint': normalizedLayoutHint,
      'requireSpecsForAcceptance': false,
    };
  }

  Map<String, dynamic> _effectiveOcrExtractionConfig(
    Map<String, dynamic> config,
    String processingMode,
  ) {
    return _applyProcessingModeDefaults(config, processingMode);
  }

  String _documentCountSummary(Map<String, dynamic> cfg) {
    final expectedCount = cfg['expectedProductCount'] as int?;
    final expectedPerFile = (cfg['expectedProductCounts'] as Map?)?.length ?? 0;
    if (expectedCount != null) {
      return 'Nombre global: $expectedCount';
    }
    if (expectedPerFile > 0) {
      return 'Nombre par fichier: $expectedPerFile defini(s)';
    }
    return 'Nombre global: libre';
  }

  String _documentConfigSummaryForMode(
    Map<String, dynamic> config,
    String processingMode,
  ) {
    final cfg = _effectiveOcrExtractionConfig(config, processingMode);
    final layoutHint = (cfg['layoutHint'] as String?) ?? 'auto';
    final countLabel = _documentCountSummary(cfg);
    if (processingMode == 'strict') {
      return 'Strict · Liste produits stricte · ${_layoutHintLabel(layoutHint)} · $countLabel';
    }
    final documentTypeHint = (cfg['documentTypeHint'] as String?) ?? 'mixed';
    return '${_documentHintLabel(documentTypeHint)} · ${_layoutHintLabel(layoutHint)} · $countLabel';
  }

  Map<String, dynamic> _attachmentExtractionRequestConfig(
    Map<String, dynamic> config,
    String processingMode,
  ) {
    final effective = _effectiveOcrExtractionConfig(config, processingMode);
    final expectedProductCount = effective['expectedProductCount'] as int?;
    final expectedProductCounts =
        (effective['expectedProductCounts'] as Map?)?.map(
      (k, v) => MapEntry(k.toString(), (v as num).toInt()),
    );
    final documentTypeHint =
        (effective['documentTypeHint'] as String?)?.trim().toLowerCase() ??
            'mixed';
    final layoutHint =
        (effective['layoutHint'] as String?)?.trim().toLowerCase() ?? 'auto';
    return {
      'expectedProductCount': expectedProductCount,
      'expectedProductCounts': expectedProductCounts,
      'strictExpectedCount': true,
      'strictCountMode': 'strict',
      'documentTypeHint': processingMode == 'strict'
          ? 'strict_produit'
          : _mapUiDocumentHintToBackend(documentTypeHint, layoutHint),
      'layoutHint': layoutHint,
      'requireSpecsForAcceptance': false,
    };
  }

  Future<void> _openEmailDetailsModal() async {
    final subjectController =
        TextEditingController(text: _subjectController.text);
    final messageController =
        TextEditingController(text: _messageController.text);
    try {
      final result = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Details email'),
          content: SizedBox(
            width: 640,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TextField(
                    controller: subjectController,
                    decoration:
                        const InputDecoration(labelText: 'Email subject'),
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    controller: messageController,
                    decoration: const InputDecoration(
                        labelText: 'Email message (HTML)'),
                    maxLines: 8,
                  ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel'),
            ),
            ElevatedButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Apply'),
            ),
          ],
        ),
      );
      if (result == true && mounted) {
        setState(() {
          _subjectController.text = subjectController.text;
          _messageController.text = messageController.text;
        });
      }
    } finally {
      subjectController.dispose();
      messageController.dispose();
    }
  }

  Future<Map<String, dynamic>?> _askExpectedCountsConfig({
    Map<String, dynamic>? initialConfig,
  }) async {
    final initial = _normalizedOcrExtractionConfig(initialConfig);
    final globalController = TextEditingController(
      text: (initial['expectedProductCount'] as int?)?.toString() ?? '',
    );
    final perFileControllers = <String, TextEditingController>{};
    final initialPerFile =
        (initial['expectedProductCounts'] as Map<String, int>?) ??
            <String, int>{};
    for (final raw in _attachments) {
      if (raw is! Map<String, dynamic>) continue;
      final fileName = (raw['file_name'] ?? '').toString().trim();
      if (fileName.isEmpty) continue;
      perFileControllers[fileName] = TextEditingController(
        text: (initialPerFile[fileName] ?? '').toString(),
      );
    }
    bool usePerFile = initialPerFile.isNotEmpty;

    final result = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setLocalState) => AlertDialog(
          title: const Text('Nombre attendu'),
          content: SizedBox(
            width: 560,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  TextField(
                    controller: globalController,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(
                      labelText: 'Nombre global',
                      hintText: 'Optionnel',
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 8),
                  CheckboxListTile(
                    value: usePerFile,
                    contentPadding: EdgeInsets.zero,
                    title: const Text('Definir un nombre par fichier'),
                    onChanged: (v) => setLocalState(() {
                      usePerFile = v ?? false;
                    }),
                  ),
                  if (usePerFile)
                    Column(
                      children: perFileControllers.entries.map((entry) {
                        return Padding(
                          padding: const EdgeInsets.only(bottom: 8),
                          child: TextField(
                            controller: entry.value,
                            keyboardType: TextInputType.number,
                            decoration: InputDecoration(
                              labelText: entry.key,
                              hintText: 'Nombre attendu pour ce fichier',
                              border: const OutlineInputBorder(),
                            ),
                          ),
                        );
                      }).toList(),
                    ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, null),
              child: const Text('Annuler'),
            ),
            ElevatedButton(
              onPressed: () {
                final expectedProductCount =
                    _parsePositiveInt(globalController.text);
                final expectedProductCounts = <String, int>{};
                if (usePerFile) {
                  for (final entry in perFileControllers.entries) {
                    final parsed = _parsePositiveInt(entry.value.text);
                    if (parsed != null) {
                      expectedProductCounts[entry.key] = parsed;
                    }
                  }
                }
                Navigator.pop(context, <String, dynamic>{
                  'expectedProductCount': expectedProductCount,
                  'expectedProductCounts': expectedProductCounts,
                });
              },
              child: const Text('Continuer'),
            ),
          ],
        ),
      ),
    );

    globalController.dispose();
    for (final ctrl in perFileControllers.values) {
      ctrl.dispose();
    }
    return result;
  }

  Future<void> _extractAttachmentsOnce() async {
    if (_selectedCampaign == null) return;
    if (_attachments.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Ajoute d\'abord au moins un PDF')),
      );
      return;
    }
    final countsConfig = await _askExpectedCountsConfig(
      initialConfig: _ocrExtractionConfig,
    );
    if (countsConfig == null) return;
    setState(() {
      _ocrExtractionConfig = _normalizedOcrExtractionConfig({
        ..._ocrExtractionConfig,
        ...countsConfig,
      });
    });

    final force = !_hasPendingAttachmentOcr;
    final confirm = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(
          force ? 'Reanalyser les fichiers ?' : 'Analyser les fichiers ?',
        ),
        content: Text(
          force
              ? "Tous les PDFs seront reanalyses avec les reglages actuels."
              : "Cette action analyse les PDFs non encore traites avec les reglages actuels.",
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Annuler'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Continuer'),
          ),
        ],
      ),
    );
    if (confirm != true) return;

    final extractionConfig = _attachmentExtractionRequestConfig(
      _ocrExtractionConfig,
      _selectedCampaignProcessingMode(),
    );

    final campaignId = _selectedCampaign!['id'] as int;
    final attachmentsToProcess = _attachments
        .whereType<Map<String, dynamic>>()
        .where((row) {
          if (force) return true;
          return row['extracted_at'] == null || row['extraction_preview'] == null;
        })
        .map((row) => <String, dynamic>{
              'id': row['id'],
              'file_name': row['file_name'],
            })
        .where((row) => row['id'] is num)
        .toList();
    if (attachmentsToProcess.isEmpty) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Aucun fichier a analyser')),
        );
      }
      return;
    }
    try {
      setState(() {
        _ocrAnalyzing = true;
        _ocrProgress = 0.0;
        _ocrStartedAt = DateTime.now();
        _ocrStatus = 'Analyse des fichiers... 0/${attachmentsToProcess.length}';
      });
      for (var i = 0; i < attachmentsToProcess.length; i++) {
        final row = attachmentsToProcess[i];
        final attachmentId = (row['id'] as num).toInt();
        final fileName = (row['file_name'] ?? '').toString().trim();
        if (mounted) {
          setState(() {
            _ocrStatus =
                'Analyse des fichiers... ${i + 1}/${attachmentsToProcess.length}${fileName.isEmpty ? '' : ' ($fileName)'}';
            _ocrProgress = i / attachmentsToProcess.length;
          });
        }
        await CampaignsApi.extractAttachmentsOnce(
          campaignId,
          force: force,
          attachmentIds: [attachmentId],
          asyncMode: true,
          expectedProductCount: extractionConfig['expectedProductCount'] as int?,
          expectedProductCounts:
              extractionConfig['expectedProductCounts'] as Map<String, int>?,
          strictExpectedCount:
              extractionConfig['strictExpectedCount'] as bool? ?? true,
          strictCountMode:
              extractionConfig['strictCountMode'] as String? ?? 'strict',
          documentTypeHint: extractionConfig['documentTypeHint'] as String?,
          layoutHint: extractionConfig['layoutHint'] as String?,
          requireSpecsForAcceptance:
              extractionConfig['requireSpecsForAcceptance'] as bool? ?? false,
        );
        if (mounted) {
          setState(() {
            _ocrProgress = (i + 1) / attachmentsToProcess.length;
          });
        }
      }
      await _waitForAttachmentExtractionCompletion(
        attachmentsToProcess
            .map((row) => (row['id'] as num).toInt())
            .toList(),
      );
      if (!mounted) return;
      setState(() {});
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            force
                ? 'Reanalyse terminee sur tous les PDFs'
                : 'Analyse terminee sur les nouveaux PDFs',
          ),
        ),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    } finally {
      if (mounted) {
        setState(() {
          _ocrAnalyzing = false;
          _ocrProgress = 0.0;
          _ocrStatus = '';
          _ocrStartedAt = null;
        });
      }
    }
  }

  void _setDisplayEmbeddingThreshold(double value) {
    final clamped =
        value.clamp(AppThresholds.minExtractionThreshold, 1.0).toDouble();
    setState(() =>
        _displayEmbeddingThreshold = double.parse(clamped.toStringAsFixed(2)));
  }

  List<double> _embeddingScores(dynamic value) {
    final out = <double>[];
    if (value is Map) {
      value.forEach((_, v) {
        if (v is List) {
          for (final entry in v) {
            if (entry is Map) {
              final score = (entry['score'] as num?)?.toDouble();
              if (score != null) out.add(score);
            }
          }
        }
      });
    }
    return out;
  }

  double _maxEmbeddingScoreFromItem(Map<String, dynamic> item) {
    final scores = <double>[
      ..._embeddingScores(item['embedding_characteristics']),
      ..._embeddingScores(item['embedding_ignored_by_regex']),
    ];
    if (scores.isEmpty) return 0.0;
    scores.sort();
    return scores.last;
  }

  String _normToken(String value) => value.toLowerCase().trim();

  void _pushCharacteristic(
    Map<String, List<String>> target,
    String rawKey,
    String rawValue,
  ) {
    final key = rawKey.trim();
    final value = rawValue.trim();
    if (key.isEmpty || value.isEmpty) return;
    final bucket = target.putIfAbsent(key, () => <String>[]);
    final exists = bucket.any((v) => _normToken(v) == _normToken(value));
    if (!exists) bucket.add(value);
  }

  Map<String, List<String>> _characteristicsFromDynamic(dynamic raw) {
    final out = <String, List<String>>{};
    if (raw is! Map) return out;
    raw.forEach((k, v) {
      final key = k.toString();
      if (v is List) {
        for (final row in v) {
          if (row is Map<String, dynamic>) {
            final value = row['value']?.toString() ?? '';
            _pushCharacteristic(out, key, value);
          } else {
            _pushCharacteristic(out, key, row.toString());
          }
        }
      } else {
        _pushCharacteristic(out, key, v.toString());
      }
    });
    return out;
  }

  Map<String, List<String>> _mergeCharacteristics(
      List<Map<String, List<String>>> maps) {
    final out = <String, List<String>>{};
    for (final map in maps) {
      map.forEach((k, values) {
        for (final value in values) {
          _pushCharacteristic(out, k, value);
        }
      });
    }
    return out;
  }

  Map<String, List<String>> _characteristicsForDisplay(
      Map<String, dynamic> item) {
    final requested =
        _characteristicsFromDynamic(item['requested_characteristics']);
    if (requested.isNotEmpty) return requested;
    return _mergeCharacteristics([
      _characteristicsFromDynamic(item['regex_characteristics']),
      _characteristicsFromDynamic(item['embedding_characteristics']),
      _characteristicsFromDynamic(item['embedding_ignored_by_regex']),
    ]);
  }

  Map<String, List<String>> _characteristicsFromStructuredProduct(dynamic raw) {
    final out = <String, List<String>>{};
    if (raw is! List) return out;
    for (final entry in raw) {
      if (entry is! Map) continue;
      final key = (entry['key'] ?? '').toString().trim();
      if (key.isEmpty) continue;
      final valuesRaw = entry['values'];
      if (valuesRaw is List) {
        for (final value in valuesRaw) {
          _pushCharacteristic(out, key, value.toString());
        }
      } else if (valuesRaw != null) {
        _pushCharacteristic(out, key, valuesRaw.toString());
      }
    }
    return out;
  }

  List<String> _accessoryLinesFromDynamic(dynamic raw) {
    final accessories = _mapListFromDynamic(raw);
    final out = <String>[];
    for (final accessory in accessories) {
      final line = _accessoryDisplayLine(accessory).trim();
      if (line.isEmpty || line == '-') continue;
      out.add(line);
    }
    return out;
  }

  Map<String, List<String>> _requestedCharacteristicsFromExtractedRow(
    Map<String, dynamic> row,
  ) {
    final out = _mergeCharacteristics([
      _characteristicsFromDynamic(row['characteristics']),
    ]);
    for (final line
        in _accessoryLinesFromDynamic(row['accessoires_structures'])) {
      _pushCharacteristic(out, 'accessoires_structures', line);
    }
    for (final line in _textsFromDynamic(row['exigences'])) {
      _pushCharacteristic(out, 'exigences', line);
    }
    return out;
  }

  List<Map<String, dynamic>> _mapListFromDynamic(dynamic raw) {
    if (raw is! List) return const [];
    final out = <Map<String, dynamic>>[];
    for (final entry in raw) {
      if (entry is Map<String, dynamic>) {
        out.add(entry);
      } else if (entry is Map) {
        out.add(entry.map((k, v) => MapEntry(k.toString(), v)));
      }
    }
    return out;
  }

  List<String> _textsFromDynamic(dynamic raw) {
    if (raw is List) {
      final out = <String>[];
      for (final entry in raw) {
        if (entry is Map) {
          final text = (entry['text'] ?? '').toString().trim();
          if (text.isNotEmpty) out.add(text);
        } else {
          final text = entry.toString().trim();
          if (text.isNotEmpty) out.add(text);
        }
      }
      return out;
    }
    final single = raw?.toString().trim() ?? '';
    if (single.isEmpty) return const [];
    return [single];
  }

  String _accessoryDisplayLine(Map<String, dynamic> accessory) {
    final parts = <String>[];
    final qty = accessory['qty'];
    if (qty != null && qty.toString().trim().isNotEmpty) {
      parts.add('${qty.toString().trim()} x');
    }
    final item = (accessory['item'] ?? '').toString().trim();
    if (item.isNotEmpty) {
      parts.add(item);
    }
    final details = (accessory['details'] ?? '').toString().trim();
    if (details.isNotEmpty) {
      parts.add(details);
    }
    final material = (accessory['material'] ?? '').toString().trim();
    if (material.isNotEmpty) {
      parts.add('materiau: $material');
    }
    final group = (accessory['group'] ?? '').toString().trim();
    if (group.isNotEmpty) {
      parts.add('groupe: $group');
    }
    if (parts.isEmpty) return '-';
    return parts.join(' | ');
  }

  Future<void> _showExtractedProductDetails(Map<String, dynamic> row) async {
    final productName = (row['product_name'] ?? '').toString().trim();
    final description = (row['description'] ?? '').toString().trim();
    final quantity = row['quantity'];
    final attachmentName = (row['attachment_name'] ?? 'PDF').toString();
    final score = (_displayScoreRaw(row) * 100).clamp(0.0, 100.0);
    final characteristics =
        (row['characteristics'] as Map<String, List<String>>?) ?? {};
    final entries = characteristics.entries.toList()
      ..sort((a, b) => a.key.compareTo(b.key));
    final accessories = _mapListFromDynamic(row['accessoires_structures']);
    final exigences = _textsFromDynamic(row['exigences']);
    final toVerify = _mapListFromDynamic(row['a_verifier']);

    await showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(productName.isEmpty ? '-' : productName),
        content: SizedBox(
          width: 760,
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Fichier: $attachmentName',
                  style: const TextStyle(fontSize: 12, color: Colors.black54),
                ),
                const SizedBox(height: 6),
                Text(
                  'Conformite: ${score.toStringAsFixed(0)}% | Qty: ${quantity ?? '-'}',
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
                if (description.isNotEmpty) ...[
                  const SizedBox(height: 12),
                  const Text(
                    'Description',
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 4),
                  Text(description),
                ],
                const SizedBox(height: 12),
                const Text(
                  'Caracteristiques',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 4),
                if (entries.isEmpty)
                  const Text('-', style: TextStyle(color: Colors.black54))
                else
                  ...entries.map(
                    (e) => Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child:
                          Text('${_prettyKey(e.key)}: ${e.value.join(' | ')}'),
                    ),
                  ),
                const SizedBox(height: 12),
                const Text(
                  'Accessoires',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 4),
                if (accessories.isEmpty)
                  const Text('-', style: TextStyle(color: Colors.black54))
                else
                  ...accessories.map(
                    (a) => Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Text(_accessoryDisplayLine(a)),
                    ),
                  ),
                const SizedBox(height: 12),
                const Text(
                  'Exigences',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 4),
                if (exigences.isEmpty)
                  const Text('-', style: TextStyle(color: Colors.black54))
                else
                  ...exigences.map(
                    (e) => Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Text(e),
                    ),
                  ),
                if (toVerify.isNotEmpty) ...[
                  const SizedBox(height: 12),
                  const Text(
                    'A verifier',
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 4),
                  ...toVerify.map(
                    (v) => Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Text(
                        '${(v['text'] ?? '').toString()}'
                        '${(v['reason'] ?? '').toString().trim().isEmpty ? '' : ' | ${(v['reason'] ?? '').toString()}'}',
                      ),
                    ),
                  ),
                ],
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

  Map<String, dynamic>? _findBestExtractedForCampaignItem(
    Map<String, dynamic> item,
    List<Map<String, dynamic>> extractedRows,
  ) {
    final target = _nameKey((item['product_name']?.toString() ?? '').trim());
    if (target.isEmpty) return null;

    Map<String, dynamic>? best;
    var bestRank = -1;
    var bestScore = -1.0;

    for (final row in extractedRows) {
      final rawName = (row['product_name']?.toString() ?? '').trim();
      final candidate = _nameKey(rawName);
      if (candidate.isEmpty) continue;

      var rank = -1;
      if (candidate == target) {
        rank = 2;
      } else if (candidate.contains(target) || target.contains(candidate)) {
        rank = 1;
      } else {
        continue;
      }

      final score = _displayScoreRaw(row);
      final isBetter =
          rank > bestRank || (rank == bestRank && score > bestScore);
      if (isBetter) {
        best = row;
        bestRank = rank;
        bestScore = score;
      }
    }
    return best;
  }

  Future<void> _showCampaignRequestedItemDetails(
    Map<String, dynamic> item, {
    Map<String, dynamic>? extractedRow,
  }) async {
    final productName = (item['product_name']?.toString() ?? '-').trim();
    final requestedQty = item['requested_qty'];
    final requestedDescription =
        (item['requested_description']?.toString() ?? '').trim();
    final productDescription =
        (item['product_description']?.toString() ?? '').trim();
    final extractedDescription =
        (extractedRow?['description']?.toString() ?? '').trim();
    final effectiveDescription = requestedDescription.isNotEmpty
        ? requestedDescription
        : (productDescription.isNotEmpty
            ? productDescription
            : extractedDescription);

    final mergedCharacteristics = _mergeCharacteristics([
      _characteristicsFromDynamic(item['requested_characteristics']),
      _characteristicsFromDynamic(item['product_characteristics']),
      if (extractedRow != null)
        _characteristicsFromDynamic(extractedRow['characteristics']),
    ]);
    final entries = mergedCharacteristics.entries.toList()
      ..sort((a, b) => a.key.compareTo(b.key));
    final accessories = extractedRow == null
        ? const <Map<String, dynamic>>[]
        : _mapListFromDynamic(extractedRow['accessoires_structures']);
    final exigences = extractedRow == null
        ? const <String>[]
        : _textsFromDynamic(extractedRow['exigences']);
    final toVerify = extractedRow == null
        ? const <Map<String, dynamic>>[]
        : _mapListFromDynamic(extractedRow['a_verifier']);

    await showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(productName.isEmpty ? '-' : productName),
        content: SizedBox(
          width: 760,
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Qty demandee: ${requestedQty ?? '-'}',
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
                if (effectiveDescription.isNotEmpty) ...[
                  const SizedBox(height: 12),
                  const Text(
                    'Description',
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 4),
                  Text(effectiveDescription),
                ],
                const SizedBox(height: 12),
                const Text(
                  'Caracteristiques',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 4),
                if (entries.isEmpty)
                  const Text('-', style: TextStyle(color: Colors.black54))
                else
                  ...entries.map(
                    (e) => Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child:
                          Text('${_prettyKey(e.key)}: ${e.value.join(' | ')}'),
                    ),
                  ),
                const SizedBox(height: 12),
                const Text(
                  'Accessoires',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 4),
                if (accessories.isEmpty)
                  const Text('-', style: TextStyle(color: Colors.black54))
                else
                  ...accessories.map(
                    (a) => Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Text(_accessoryDisplayLine(a)),
                    ),
                  ),
                const SizedBox(height: 12),
                const Text(
                  'Exigences',
                  style: TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 4),
                if (exigences.isEmpty)
                  const Text('-', style: TextStyle(color: Colors.black54))
                else
                  ...exigences.map(
                    (e) => Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Text(e),
                    ),
                  ),
                if (toVerify.isNotEmpty) ...[
                  const SizedBox(height: 12),
                  const Text(
                    'A verifier',
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 4),
                  ...toVerify.map(
                    (v) => Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Text(
                        '${(v['text'] ?? '').toString()}'
                        '${(v['reason'] ?? '').toString().trim().isEmpty ? '' : ' | ${(v['reason'] ?? '').toString()}'}',
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Fermer'),
          ),
          ElevatedButton.icon(
            onPressed: () {
              Navigator.of(context).pop();
              _editCampaignRequestedItem(item);
            },
            icon: const Icon(Icons.edit_outlined),
            label: const Text('Modifier'),
          ),
        ],
      ),
    );
  }

  String _prettyKey(String key) {
    final clean = key.replaceAll('_', ' ').trim();
    if (clean.isEmpty) return '-';
    return clean[0].toUpperCase() + clean.substring(1);
  }

  String _simpleNorm(String text) {
    return text
        .toLowerCase()
        .replaceAll('Ã©', 'e')
        .replaceAll('Ã¨', 'e')
        .replaceAll('Ãª', 'e')
        .replaceAll('Ã«', 'e')
        .replaceAll('Ã ', 'a')
        .replaceAll('Ã¢', 'a')
        .replaceAll('Ã®', 'i')
        .replaceAll('Ã¯', 'i')
        .replaceAll('Ã´', 'o')
        .replaceAll('Ã¶', 'o')
        .replaceAll('Ã»', 'u')
        .replaceAll('Ã¼', 'u')
        .replaceAll('Ã§', 'c')
        .replaceAll(RegExp(r'\s+'), ' ')
        .trim();
  }

  String _nameKey(String text) {
    return _simpleNorm(text).replaceAll(RegExp(r'[^a-z0-9 ]'), '').trim();
  }

  String _extractedRowKey(Map<String, dynamic> row) {
    final file = row['attachment_name']?.toString() ?? '';
    final name = row['product_name']?.toString() ?? '';
    return '$file|$name';
  }

  int? _findProductIdByName(String productName) {
    final target = _nameKey(productName);
    if (target.isEmpty) return null;
    for (final product in _products) {
      if (product is! Map<String, dynamic>) continue;
      final name = _nameKey(product['name']?.toString() ?? '');
      if (name == target) {
        return product['id'] as int?;
      }
    }
    return null;
  }

  bool _campaignHasRequestedProductName(String productName) {
    final target = _nameKey(productName);
    if (target.isEmpty) return false;
    for (final item in _campaignItems) {
      if (item is! Map<String, dynamic>) continue;
      final existingName =
          _nameKey((item['product_name']?.toString() ?? '').trim());
      if (existingName.isNotEmpty && existingName == target) {
        return true;
      }
      final existingProductId = item['product_id'];
      final catalogProductId = _findProductIdByName(productName);
      if (existingProductId is int &&
          catalogProductId != null &&
          existingProductId == catalogProductId) {
        return true;
      }
    }
    return false;
  }

  Map<String, dynamic> _campaignItemToPayload(Map<String, dynamic> item) {
    return {
      'product_id': item['product_id'],
      'requested_qty': item['requested_qty'],
      'requested_description': item['requested_description'],
      'requested_characteristics': item['requested_characteristics'],
      'requested_embedding_score': item['requested_embedding_score'],
    };
  }

  Future<void> _confirmExtractedIntoCampaign(Map<String, dynamic> row) async {
    if (_selectedCampaign == null) return;
    final productName = (row['product_name']?.toString() ?? '').trim();
    if (productName.isEmpty) return;
    if (_campaignHasRequestedProductName(productName)) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Produit deja present: $productName')),
        );
      }
      return;
    }

    final rowKey = _extractedRowKey(row);
    if (_confirmingExtractedKeys.contains(rowKey)) return;

    setState(() => _confirmingExtractedKeys.add(rowKey));
    try {
      final description = (row['description']?.toString() ?? '').trim();
      final normalizedExtractedChars =
          _requestedCharacteristicsFromExtractedRow(row);
      final normalizedExtractedCharsStorage =
          _characteristicsToStorageMap(normalizedExtractedChars);
      final extractedQty = (row['quantity'] as num?)?.toDouble();
      final embeddingScore =
          ((row['embedding_max_score'] as num?)?.toDouble() ?? 0.0);
      var productId = _findProductIdByName(productName);
      if (productId == null) {
        final created = await ProductsApi.create({
          'name': productName,
        });
        productId = (created['id'] as num?)?.toInt();
      }
      if (productId == null) {
        throw Exception('Product creation failed (missing id)');
      }
      _products = await ProductsApi.list();

      final itemsPayload = <Map<String, dynamic>>[];
      for (final item in _campaignItems) {
        if (item is! Map<String, dynamic>) continue;
        itemsPayload.add(_campaignItemToPayload(item));
      }
      itemsPayload.add({
        'product_id': productId,
        'requested_qty': extractedQty,
        if (description.isNotEmpty) 'requested_description': description,
        'requested_characteristics': normalizedExtractedCharsStorage,
        if (embeddingScore > 0.0) 'requested_embedding_score': embeddingScore,
      });

      final dedupByProductId = <int, Map<String, dynamic>>{};
      for (final payload in itemsPayload) {
        final pid = payload['product_id'];
        if (pid is int) dedupByProductId[pid] = payload;
      }

      await CampaignsApi.setItems(
          _selectedCampaign!['id'] as int, dedupByProductId.values.toList());
      await _loadCampaignItems();
      if (!mounted) return;
      setState(() {});
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('Produit ajoute: $productName'),
        ),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    } finally {
      if (mounted) {
        setState(() => _confirmingExtractedKeys.remove(rowKey));
      }
    }
  }

  Future<void> _confirmAllExtractedIntoCampaign() async {
    if (_selectedCampaign == null || _confirmingAllExtracted) return;

    final extracted = _filteredExtractedItems();
    final rows = extracted.where((row) {
      final productName = (row['product_name']?.toString() ?? '').trim();
      return productName.isNotEmpty;
    }).toList();
    if (rows.isEmpty) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('Aucun produit extrait valide a ajouter.'),
          ),
        );
      }
      return;
    }

    final rowKeys = rows.map(_extractedRowKey).toSet();
    setState(() {
      _confirmingAllExtracted = true;
      _confirmingExtractedKeys.addAll(rowKeys);
    });

    var createdProducts = 0;
    var addedCampaignItems = 0;
    var skippedDuplicates = 0;

    try {
      final campaignId = _selectedCampaign!['id'] as int;
      final payloadByProductId = <int, Map<String, dynamic>>{};
      final requestedNameKeys = <String>{};
      final productIdByNameKey = <String, int>{};
      for (final product in _products.whereType<Map<String, dynamic>>()) {
        final name = (product['name']?.toString() ?? '').trim();
        final key = _nameKey(name);
        final id = product['id'];
        if (key.isNotEmpty && id is int) {
          productIdByNameKey[key] = id;
        }
      }
      for (final raw in _campaignItems) {
        if (raw is! Map<String, dynamic>) continue;
        final pid = raw['product_id'];
        if (pid is! int) continue;
        payloadByProductId[pid] = _campaignItemToPayload(raw);
        final existingName = (raw['product_name']?.toString() ?? '').trim();
        final existingKey = _nameKey(existingName);
        if (existingKey.isNotEmpty) {
          requestedNameKeys.add(existingKey);
        }
      }

      var productsChanged = false;
      for (final row in rows) {
        final productName = (row['product_name']?.toString() ?? '').trim();
        if (productName.isEmpty) continue;
        final productNameKey = _nameKey(productName);
        if (productNameKey.isEmpty) continue;
        if (requestedNameKeys.contains(productNameKey)) {
          skippedDuplicates += 1;
          continue;
        }

        final description = (row['description']?.toString() ?? '').trim();
        final normalizedExtractedChars =
            _requestedCharacteristicsFromExtractedRow(row);
        final normalizedExtractedCharsStorage =
            _characteristicsToStorageMap(normalizedExtractedChars);
        final extractedQty = (row['quantity'] as num?)?.toDouble();
        final embeddingScore =
            ((row['embedding_max_score'] as num?)?.toDouble() ?? 0.0);
        var productId = productIdByNameKey[productNameKey];
        if (productId == null) {
          final created = await ProductsApi.create({
            'name': productName,
          });
          productId = (created['id'] as num?)?.toInt();
          if (productId != null) {
            createdProducts += 1;
            productsChanged = true;
            productIdByNameKey[productNameKey] = productId;
          }
        }
        if (productId == null) {
          continue;
        }
        payloadByProductId[productId] = {
          'product_id': productId,
          'requested_qty': extractedQty,
          if (description.isNotEmpty) 'requested_description': description,
          'requested_characteristics': normalizedExtractedCharsStorage,
          if (embeddingScore > 0.0) 'requested_embedding_score': embeddingScore,
        };
        addedCampaignItems += 1;
        requestedNameKeys.add(productNameKey);
      }

      await CampaignsApi.setItems(
          campaignId, payloadByProductId.values.toList());
      if (productsChanged) {
        _products = await ProductsApi.list();
      }
      await _loadCampaignItems();
      if (!mounted) return;
      setState(() {});
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            'Ajout en lot termine: '
            '$addedCampaignItems ajoutes, '
            '$createdProducts produits crees, '
            '$skippedDuplicates deja presents.',
          ),
        ),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    } finally {
      if (mounted) {
        setState(() {
          _confirmingAllExtracted = false;
          _confirmingExtractedKeys.removeAll(rowKeys);
        });
      }
    }
  }

  bool _looksLikeExtractedProduct(
    Map<String, dynamic> item,
    Map<String, List<String>> characteristics,
  ) {
    final docType = (item['doc_type']?.toString() ?? '').trim().toLowerCase();
    if (docType == 'product_list') {
      final rawName = (item['product_name']?.toString() ?? '').trim();
      return rawName.isNotEmpty && rawName.length >= 3;
    }

    final rawName = (item['product_name']?.toString() ?? '').trim();
    if (rawName.isEmpty || rawName.length < 4) return false;

    final name = _simpleNorm(rawName);
    final hasKeyword = _productNameKeywords.any(name.contains);
    if (_nonProductTitlePrefixes.any(name.startsWith) && !hasKeyword) {
      return false;
    }

    if (hasKeyword) return true;

    final words = rawName.split(RegExp(r'\s+')).where((w) => w.isNotEmpty);
    final charsCount =
        characteristics.values.fold<int>(0, (sum, list) => sum + list.length);
    return words.length >= 2 && words.length <= 7 && charsCount >= 2;
  }

  List<Map<String, dynamic>> _pdfExtractedItems() {
    final accepted = <Map<String, dynamic>>[];
    final fallback = <Map<String, dynamic>>[];
    for (final att in _attachments) {
      if (att is! Map<String, dynamic>) continue;
      final preview = _attachmentPreviewAsMap(att);
      if (preview == null) continue;
      final attachmentName = att['file_name']?.toString() ?? 'PDF';
      final previewDocType = preview['doc_type']?.toString();

      final products = preview['products'];
      if (products is List && products.isNotEmpty) {
        for (final rawProduct in products) {
          if (rawProduct is! Map) continue;
          final product = rawProduct is Map<String, dynamic>
              ? rawProduct
              : rawProduct.map((k, v) => MapEntry(k.toString(), v));
          final mergedCharacteristics =
              _characteristicsFromStructuredProduct(product['characteristics']);
          final qualityScore =
              (product['quality_score'] as num?)?.toDouble() ?? 0.0;
          final row = <String, dynamic>{
            'attachment_name': attachmentName,
            'product_name': product['name']?.toString() ?? '-',
            'description': product['description']?.toString() ?? '',
            'quantity': (product['quantity'] as num?)?.toDouble(),
            'regex_score': qualityScore,
            'doc_type': previewDocType,
            'requested_characteristics': null,
            'characteristics': mergedCharacteristics,
            'embedding_max_score': qualityScore,
            'accessoires_structures': _mapListFromDynamic(
              product['accessoires_structures'],
            ),
            'exigences': product['exigences'],
            'a_verifier': product['a_verifier'],
            'structured_source': true,
          };
          fallback.add(row);
          // For structured `products` payloads, keep full recall and do not
          // hide model-detected products via UI heuristics.
          accepted.add(row);
        }
        continue;
      }

      final items = preview['items'];
      if (items is! List) continue;
      for (final rawItem in items) {
        if (rawItem is! Map) continue;
        final item = rawItem is Map<String, dynamic>
            ? rawItem
            : rawItem.map((k, v) => MapEntry(k.toString(), v));
        final mergedCharacteristics = _characteristicsForDisplay(item);
        final row = <String, dynamic>{
          'attachment_name': attachmentName,
          'product_name': item['product_name']?.toString() ?? '-',
          'description': item['description']?.toString() ?? '',
          'quantity': (item['quantity'] as num?)?.toDouble(),
          'regex_score': (item['regex_score'] as num?)?.toDouble() ?? 0.0,
          'doc_type': item['doc_type']?.toString() ?? previewDocType,
          'requested_characteristics': item['requested_characteristics'],
          'characteristics': mergedCharacteristics,
          'embedding_max_score': _maxEmbeddingScoreFromItem(item),
          'accessoires_structures': const <Map<String, dynamic>>[],
          'exigences': const <String>[],
          'a_verifier': const <Map<String, dynamic>>[],
        };
        fallback.add(row);
        if (_looksLikeExtractedProduct(item, mergedCharacteristics)) {
          accepted.add(row);
        }
      }
    }
    return accepted.isNotEmpty ? accepted : fallback;
  }

  Widget _buildExtractedProductCard(
    Map<String, dynamic> row, {
    bool namesOnly = false,
  }) {
    final productName = row['product_name']?.toString().trim();
    final description = row['description']?.toString().trim() ?? '';
    final attachmentName = row['attachment_name']?.toString() ?? 'PDF';
    final score = (_displayScoreRaw(row) * 100).clamp(0.0, 100.0);
    final characteristics =
        (row['characteristics'] as Map<String, List<String>>?) ?? {};
    final entries = characteristics.entries.toList()
      ..sort((a, b) => a.key.compareTo(b.key));
    final characteristicsCount =
        entries.fold<int>(0, (sum, e) => sum + e.value.length);
    final accessories = _mapListFromDynamic(row['accessoires_structures']);
    final exigences = _textsFromDynamic(row['exigences']);
    final rowKey = _extractedRowKey(row);
    final isConfirming = _confirmingExtractedKeys.contains(rowKey);
    final alreadyAdded = productName != null && productName.isNotEmpty
        ? _campaignHasRequestedProductName(productName)
        : false;

    return GestureDetector(
      onTap: () => _showExtractedProductDetails(row),
      child: Card(
        margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        elevation: 0.5,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(
                    child: Text(
                      (productName == null || productName.isEmpty)
                          ? '-'
                          : productName,
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w700,
                        height: 1.2,
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                    decoration: BoxDecoration(
                      color: Colors.teal.shade50,
                      borderRadius: BorderRadius.circular(999),
                      border: Border.all(color: Colors.teal.shade100),
                    ),
                    child: Text(
                      'Conformite ${score.toStringAsFixed(0)}%',
                      style: const TextStyle(
                        fontSize: 12,
                        fontWeight: FontWeight.w700,
                        color: Colors.teal,
                      ),
                    ),
                  ),
                  if (!namesOnly) ...[
                    const SizedBox(width: 8),
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 10, vertical: 6),
                      decoration: BoxDecoration(
                        color: Colors.blueGrey.shade50,
                        borderRadius: BorderRadius.circular(999),
                        border: Border.all(color: Colors.blueGrey.shade100),
                      ),
                      child: Text(
                        '$characteristicsCount caracteristiques',
                        style: const TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w700,
                          color: Colors.black87,
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 10, vertical: 6),
                      decoration: BoxDecoration(
                        color: Colors.orange.shade50,
                        borderRadius: BorderRadius.circular(999),
                        border: Border.all(color: Colors.orange.shade100),
                      ),
                      child: Text(
                        '${accessories.length} accessoires',
                        style: const TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w700,
                          color: Colors.black87,
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 10, vertical: 6),
                      decoration: BoxDecoration(
                        color: Colors.purple.shade50,
                        borderRadius: BorderRadius.circular(999),
                        border: Border.all(color: Colors.purple.shade100),
                      ),
                      child: Text(
                        '${exigences.length} exigences',
                        style: const TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w700,
                          color: Colors.black87,
                        ),
                      ),
                    ),
                  ],
                ],
              ),
              const SizedBox(height: 8),
              Text(
                'Fichier: $attachmentName',
                style: const TextStyle(fontSize: 12, color: Colors.black54),
              ),
              const SizedBox(height: 6),
              const Text(
                'Cliquer pour afficher le detail complet',
                style: TextStyle(fontSize: 12, color: Colors.black54),
              ),
              const SizedBox(height: 8),
              Row(
                children: [
                  ElevatedButton.icon(
                    onPressed: isConfirming || alreadyAdded
                        ? null
                        : () => _confirmExtractedIntoCampaign(row),
                    icon: isConfirming
                        ? const SizedBox(
                            width: 14,
                            height: 14,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.check_circle_outline),
                    label: Text(
                      isConfirming
                          ? 'Ajout...'
                          : alreadyAdded
                              ? 'Deja ajoute'
                              : 'Confirmer vers produits demandes',
                    ),
                  ),
                ],
              ),
              if (!namesOnly && accessories.isNotEmpty) ...[
                const SizedBox(height: 12),
                const Text(
                  'Accessoires',
                  style: TextStyle(fontWeight: FontWeight.w700, fontSize: 14),
                ),
                const SizedBox(height: 8),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Colors.white,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: Colors.grey.shade300),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: accessories
                        .map(
                          (a) => Padding(
                            padding: const EdgeInsets.symmetric(vertical: 3),
                            child: Text(
                              _accessoryDisplayLine(a),
                              style: const TextStyle(
                                fontSize: 13,
                                height: 1.35,
                              ),
                            ),
                          ),
                        )
                        .toList(),
                  ),
                ),
              ],
              if (!namesOnly && exigences.isNotEmpty) ...[
                const SizedBox(height: 12),
                const Text(
                  'Exigences',
                  style: TextStyle(fontWeight: FontWeight.w700, fontSize: 14),
                ),
                const SizedBox(height: 8),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Colors.white,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: Colors.grey.shade300),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: exigences
                        .map(
                          (e) => Padding(
                            padding: const EdgeInsets.symmetric(vertical: 3),
                            child: Text(
                              e,
                              style: const TextStyle(
                                fontSize: 13,
                                height: 1.35,
                              ),
                            ),
                          ),
                        )
                        .toList(),
                  ),
                ),
              ],
              if (!namesOnly && description.isNotEmpty) ...[
                const SizedBox(height: 10),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Colors.grey.shade50,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: Colors.grey.shade300),
                  ),
                  child: Text(
                    description,
                    style: const TextStyle(fontSize: 14, height: 1.35),
                  ),
                ),
              ],
              if (!namesOnly && entries.isNotEmpty) ...[
                const SizedBox(height: 12),
                const Text(
                  'Caracteristiques detectees',
                  style: TextStyle(fontWeight: FontWeight.w700, fontSize: 14),
                ),
                const SizedBox(height: 8),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Colors.white,
                    borderRadius: BorderRadius.circular(10),
                    border: Border.all(color: Colors.grey.shade300),
                  ),
                  child: Column(
                    children: entries
                        .map(
                          (e) => Padding(
                            padding: const EdgeInsets.symmetric(vertical: 4),
                            child: Row(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                SizedBox(
                                  width: 170,
                                  child: Text(
                                    _prettyKey(e.key),
                                    style: const TextStyle(
                                      fontWeight: FontWeight.w600,
                                      fontSize: 13,
                                    ),
                                  ),
                                ),
                                const SizedBox(width: 8),
                                Expanded(
                                  child: Text(
                                    e.value.join(' | '),
                                    style: const TextStyle(
                                      fontSize: 13,
                                      height: 1.35,
                                    ),
                                  ),
                                ),
                              ],
                            ),
                          ),
                        )
                        .toList(),
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  List<Map<String, dynamic>> _filteredExtractedItems() {
    final extractedAll = _pdfExtractedItems();
    final threshold = _displayEmbeddingThreshold;
    return extractedAll.where((row) {
      if ((row['structured_source'] as bool?) == true) {
        return true;
      }
      final docType = (row['doc_type']?.toString() ?? '').trim().toLowerCase();
      if (docType == 'product_list') {
        return true;
      }
      final score = _displayScoreRaw(row);
      return score >= threshold;
    }).toList();
  }

  double _displayScoreRaw(Map<String, dynamic> row) {
    final emb = ((row['embedding_max_score'] as num?)?.toDouble() ?? 0.0)
        .clamp(0.0, 1.0);
    final regex =
        ((row['regex_score'] as num?)?.toDouble() ?? 0.0).clamp(0.0, 1.0);
    return emb > regex ? emb : regex;
  }

  Widget _buildExtractedItemsPane({
    bool showExpandButton = false,
    VoidCallback? onThresholdChanged,
    bool namesOnly = false,
  }) {
    final extractedAll = _pdfExtractedItems();
    final extracted = _filteredExtractedItems();

    if (extractedAll.isEmpty) {
      return const Card(
        margin: EdgeInsets.symmetric(horizontal: 4),
        child: Padding(
          padding: EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'Produits extraits depuis les PDFs joints',
                style: TextStyle(fontWeight: FontWeight.w700, fontSize: 16),
              ),
              SizedBox(height: 8),
              Text(
                'Aucune extraction disponible. Joins un PDF puis lance l\'analyse OCR.',
                style: TextStyle(color: Colors.black54),
              ),
            ],
          ),
        ),
      );
    }

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 4),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
      child: Padding(
        padding: const EdgeInsets.all(10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: Colors.teal.shade50,
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: Colors.teal.shade100),
              ),
              child: Row(
                children: [
                  const Icon(Icons.document_scanner, color: Colors.teal),
                  const SizedBox(width: 8),
                  const Expanded(
                    child: Text(
                      'Produits extraits depuis les PDFs joints',
                      style:
                          TextStyle(fontWeight: FontWeight.w700, fontSize: 15),
                    ),
                  ),
                  Text(
                    '${extracted.length}/${extractedAll.length}',
                    style: const TextStyle(
                      fontWeight: FontWeight.w700,
                      color: Colors.teal,
                    ),
                  ),
                  const SizedBox(width: 8),
                  ElevatedButton.icon(
                    onPressed: (_confirmingAllExtracted || extracted.isEmpty)
                        ? null
                        : _confirmAllExtractedIntoCampaign,
                    icon: _confirmingAllExtracted
                        ? const SizedBox(
                            width: 14,
                            height: 14,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.playlist_add_check),
                    label: Text(
                      _confirmingAllExtracted
                          ? 'Ajout en cours...'
                          : 'Ajouter tout',
                    ),
                  ),
                  if (showExpandButton) ...[
                    const SizedBox(width: 8),
                    OutlinedButton.icon(
                      onPressed: _openExtractedItemsFullscreen,
                      icon: const Icon(Icons.open_in_full),
                      label: const Text('Agrandir'),
                    ),
                  ],
                ],
              ),
            ),
            const SizedBox(height: 8),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 4),
              child: Row(
                children: [
                  IconButton(
                    tooltip: 'Baisser le seuil',
                    onPressed: () {
                      _setDisplayEmbeddingThreshold(
                          _displayEmbeddingThreshold - 0.05);
                      onThresholdChanged?.call();
                    },
                    icon: const Icon(Icons.remove_circle_outline),
                  ),
                  Expanded(
                    child: Slider(
                      value: _displayEmbeddingThreshold,
                      min: AppThresholds.minExtractionThreshold,
                      max: 1,
                      divisions: 16,
                      label:
                          '${(_displayEmbeddingThreshold * 100).toStringAsFixed(0)}%',
                      onChanged: (value) {
                        _setDisplayEmbeddingThreshold(value);
                        onThresholdChanged?.call();
                      },
                    ),
                  ),
                  IconButton(
                    tooltip: 'Monter le seuil',
                    onPressed: () {
                      _setDisplayEmbeddingThreshold(
                          _displayEmbeddingThreshold + 0.05);
                      onThresholdChanged?.call();
                    },
                    icon: const Icon(Icons.add_circle_outline),
                  ),
                  const SizedBox(width: 4),
                  Text(
                    '${(_displayEmbeddingThreshold * 100).toStringAsFixed(0)}%',
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(width: 8),
                  TextButton(
                    onPressed: () {
                      _setDisplayEmbeddingThreshold(
                        AppThresholds.minExtractionThreshold,
                      );
                      onThresholdChanged?.call();
                    },
                    child: Text(
                      'Reset ${(AppThresholds.minExtractionThreshold * 100).toStringAsFixed(0)}%',
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 6),
            Expanded(
              child: extracted.isEmpty
                  ? const Center(
                      child: Text(
                        'Aucune ligne n\'atteint ce seuil d\'affichage.',
                        style: TextStyle(color: Colors.black54),
                      ),
                    )
                  : ListView.builder(
                      itemCount: extracted.length,
                      itemBuilder: (context, index) =>
                          _buildExtractedProductCard(
                        extracted[index],
                        namesOnly: namesOnly,
                      ),
                    ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildCampaignRequestedItemsPane() {
    final extractedRows = _pdfExtractedItems();

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 4),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Expanded(
                  child: Text(
                    'Produits demandes dans la campagne',
                    style: TextStyle(fontWeight: FontWeight.w700),
                  ),
                ),
                OutlinedButton.icon(
                  onPressed: _addCampaignRequestedItem,
                  icon: const Icon(Icons.add),
                  label: const Text('Ajouter'),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Expanded(
              child: _campaignItems.isEmpty
                  ? const Center(
                      child: Text(
                        'Aucun produit demande pour cette campagne.',
                        style: TextStyle(color: Colors.black54),
                      ),
                    )
                  : ListView.builder(
                      itemCount: _campaignItems.length,
                      itemBuilder: (context, index) {
                        final item = _campaignItems[index];
                        if (item is! Map<String, dynamic>) {
                          return const SizedBox.shrink();
                        }
                        final extractedRow = _findBestExtractedForCampaignItem(
                          item,
                          extractedRows,
                        );
                        final chars = _characteristicsFromDynamic(
                          item['requested_characteristics'],
                        );
                        final charsCount = chars.values.fold<int>(
                          0,
                          (sum, list) => sum + list.length,
                        );
                        final requestedQty = item['requested_qty'];
                        final requestedDescription =
                            (item['requested_description']?.toString() ?? '')
                                .trim();
                        final previewKeys = chars.keys
                            .take(3)
                            .map(_prettyKey)
                            .toList()
                            .join(', ');
                        final accessoriesCount = extractedRow == null
                            ? 0
                            : _mapListFromDynamic(
                                extractedRow['accessoires_structures'],
                              ).length;
                        final exigencesCount = extractedRow == null
                            ? 0
                            : _textsFromDynamic(extractedRow['exigences'])
                                .length;
                        final subtitleParts = <String>[
                          'Qty: ${requestedQty ?? '-'}',
                          'Caracteristiques: $charsCount',
                        ];
                        if (accessoriesCount > 0) {
                          subtitleParts.add('Accessoires: $accessoriesCount');
                        }
                        if (exigencesCount > 0) {
                          subtitleParts.add('Exigences: $exigencesCount');
                        }
                        if (previewKeys.isNotEmpty) {
                          subtitleParts.add('Cles: $previewKeys');
                        }
                        if (requestedDescription.isNotEmpty) {
                          subtitleParts.add('Desc: $requestedDescription');
                        }
                        return ListTile(
                          dense: true,
                          title: Text(
                            item['product_name'] as String? ??
                                'Product ${item['product_id']}',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                          subtitle: Text(
                            '${subtitleParts.join(' | ')}\nCliquer pour details',
                            maxLines: 3,
                            overflow: TextOverflow.ellipsis,
                          ),
                          trailing: Wrap(
                            spacing: 4,
                            children: [
                              IconButton(
                                tooltip: 'Modifier',
                                icon: const Icon(Icons.edit_outlined, size: 18),
                                onPressed: () =>
                                    _editCampaignRequestedItem(item),
                              ),
                              IconButton(
                                tooltip: 'Supprimer',
                                icon:
                                    const Icon(Icons.delete_outline, size: 18),
                                onPressed: () =>
                                    _removeCampaignRequestedItem(item),
                              ),
                            ],
                          ),
                          onTap: () => _showCampaignRequestedItemDetails(
                            item,
                            extractedRow: extractedRow,
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

  Map<String, dynamic>? _characteristicsToStorageMap(
      Map<String, List<String>> map) {
    if (map.isEmpty) return null;
    final out = <String, List<Map<String, String>>>{};
    for (final entry in map.entries) {
      final key = entry.key.trim();
      if (key.isEmpty) continue;
      final values = <Map<String, String>>[];
      final seen = <String>{};
      for (final raw in entry.value) {
        final value = raw.trim();
        if (value.isEmpty) continue;
        final token = value.toLowerCase();
        if (seen.contains(token)) continue;
        seen.add(token);
        values.add({'value': value});
      }
      if (values.isNotEmpty) {
        out[key] = values;
      }
    }
    if (out.isEmpty) return null;
    return out;
  }

  Future<Map<String, dynamic>?> _resolveRequestedCharacteristicsFromInputs({
    required String keyValueText,
    required String rawText,
  }) async {
    final manual = _characteristicsFromEditorText(keyValueText);
    final raw = rawText.trim();
    if (raw.isEmpty) return manual;

    try {
      final parsed = await CampaignsApi.parseCharacteristicsFromText(raw);
      final auto = parsed['requested_characteristics'];
      if (manual == null && auto is Map) {
        return auto.cast<String, dynamic>();
      }
      final merged = _mergeCharacteristics([
        _characteristicsFromDynamic(manual),
        _characteristicsFromDynamic(auto),
      ]);
      return _characteristicsToStorageMap(merged);
    } catch (_) {
      return manual;
    }
  }

  Future<void> _addCampaignRequestedItem() async {
    if (_selectedCampaign == null) return;
    final campaignId = _selectedCampaign!['id'] as int;
    final nameController = TextEditingController();
    final unitController = TextEditingController();
    final qtyController = TextEditingController();
    final descController = TextEditingController();
    final charsController = TextEditingController();
    final rawTextController = TextEditingController();

    final action = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Ajouter produit demande'),
        content: SizedBox(
          width: 700,
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                TextField(
                  controller: nameController,
                  decoration: const InputDecoration(labelText: 'Nom produit'),
                ),
                TextField(
                  controller: unitController,
                  decoration: const InputDecoration(labelText: 'Unite'),
                ),
                SizedBox(
                  width: 180,
                  child: TextField(
                    controller: qtyController,
                    decoration:
                        const InputDecoration(labelText: 'Requested qty'),
                    keyboardType: TextInputType.number,
                  ),
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: descController,
                  decoration:
                      const InputDecoration(labelText: 'Requested description'),
                  maxLines: 3,
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: charsController,
                  decoration: const InputDecoration(
                    labelText:
                        'Requested characteristics (one per line: key: value)',
                  ),
                  maxLines: 7,
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: rawTextController,
                  decoration: const InputDecoration(
                    labelText:
                        'Texte brut fiche technique (optionnel, extraction auto)',
                  ),
                  maxLines: 6,
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, 'cancel'),
            child: const Text('Annuler'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(context, 'save'),
            child: const Text('Ajouter'),
          ),
        ],
      ),
    );

    try {
      if (action != 'save') return;

      final name = nameController.text.trim();
      final unit = unitController.text.trim();
      final qtyText = qtyController.text.trim();
      final description = descController.text.trim();
      final characteristics = await _resolveRequestedCharacteristicsFromInputs(
        keyValueText: charsController.text.trim(),
        rawText: rawTextController.text.trim(),
      );
      if (name.isEmpty) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text('Le nom du produit est obligatoire')),
          );
        }
        return;
      }

      int? productId = _findProductIdByName(name);
      if (productId == null) {
        final created = await ProductsApi.create({
          'name': name,
          if (unit.isNotEmpty) 'unit': unit,
          if (description.isNotEmpty) 'description': description,
          if (characteristics != null && characteristics.isNotEmpty)
            'characteristics': characteristics,
        });
        productId = created['id'] as int;
        _products = await ProductsApi.list();
      } else {
        final existing = _products.whereType<Map<String, dynamic>>().firstWhere(
              (p) => p['id'] == productId,
              orElse: () => <String, dynamic>{},
            );
        final existingUnit = (existing['unit']?.toString() ?? '').trim();
        if (unit.isNotEmpty && existingUnit.isEmpty) {
          await ProductsApi.update(productId, {'unit': unit});
          _products = await ProductsApi.list();
        }
      }

      final rows = <Map<String, dynamic>>[];
      for (final raw in _campaignItems) {
        if (raw is! Map<String, dynamic>) continue;
        rows.add(_campaignItemToPayload(raw));
      }
      rows.add({
        'product_id': productId,
        'requested_qty': qtyText.isEmpty ? null : double.tryParse(qtyText),
        'requested_description': description.isEmpty ? null : description,
        'requested_characteristics': characteristics,
      });

      final dedupByProductId = <int, Map<String, dynamic>>{};
      for (final row in rows) {
        final pid = row['product_id'];
        if (pid is int) dedupByProductId[pid] = row;
      }
      await CampaignsApi.setItems(campaignId, dedupByProductId.values.toList());
      await _loadCampaignItems();
      if (!mounted) return;
      setState(() {});
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Produit ajoute a la campagne')),
      );
    } catch (err) {
      if (mounted) _showError(err);
    } finally {
      nameController.dispose();
      unitController.dispose();
      qtyController.dispose();
      descController.dispose();
      charsController.dispose();
      rawTextController.dispose();
    }
  }

  Future<void> _removeCampaignRequestedItem(Map<String, dynamic> item) async {
    if (_selectedCampaign == null) return;
    final campaignId = _selectedCampaign!['id'] as int;
    final confirm = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Supprimer produit'),
        content: Text(
          'Supprimer "${item['product_name'] ?? 'ce produit'}" de la campagne ?',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Annuler'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Supprimer'),
          ),
        ],
      ),
    );
    if (confirm != true) return;

    try {
      final campaignItemId = item['id'] as int?;
      final productId = item['product_id'] as int?;
      final rows = <Map<String, dynamic>>[];
      for (final raw in _campaignItems) {
        if (raw is! Map<String, dynamic>) continue;
        final sameRow = campaignItemId != null
            ? raw['id'] == campaignItemId
            : (productId != null && raw['product_id'] == productId);
        if (sameRow) continue;
        rows.add(_campaignItemToPayload(raw));
      }
      await CampaignsApi.setItems(campaignId, rows);
      await _loadCampaignItems();
      if (!mounted) return;
      setState(() {});
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Produit retire de la campagne')),
      );
    } catch (err) {
      if (mounted) _showError(err);
    }
  }

  Future<void> _editCampaignRequestedItem(Map<String, dynamic> item) async {
    if (_selectedCampaign == null) return;
    final campaignId = _selectedCampaign!['id'] as int;
    final campaignItemId = item['id'] as int?;
    final currentProductId = item['product_id'] as int?;
    if (currentProductId == null) return;

    final product = _products.whereType<Map<String, dynamic>>().firstWhere(
          (p) => p['id'] == currentProductId,
          orElse: () => <String, dynamic>{},
        );

    final nameController = TextEditingController(
      text: (item['product_name']?.toString() ??
              product['name']?.toString() ??
              '')
          .trim(),
    );
    final unitController = TextEditingController(
      text: (product['unit']?.toString() ?? '').trim(),
    );
    final qtyController = TextEditingController(
      text: item['requested_qty']?.toString() ?? '',
    );
    final descController = TextEditingController(
      text:
          ((item['requested_description']?.toString() ?? '').trim().isNotEmpty)
              ? (item['requested_description']?.toString() ?? '').trim()
              : (item['product_description']?.toString() ?? '').trim(),
    );
    final charsController = TextEditingController(
      text: _characteristicsToEditorText(
        item['requested_characteristics'] ?? item['product_characteristics'],
      ),
    );
    final rawTextController = TextEditingController();

    final action = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Modifier produit demande'),
        content: SizedBox(
          width: 700,
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                TextField(
                  controller: nameController,
                  decoration: const InputDecoration(labelText: 'Nom produit'),
                ),
                TextField(
                  controller: unitController,
                  decoration: const InputDecoration(labelText: 'Unite'),
                ),
                SizedBox(
                  width: 180,
                  child: TextField(
                    controller: qtyController,
                    decoration:
                        const InputDecoration(labelText: 'Requested qty'),
                    keyboardType: TextInputType.number,
                  ),
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: descController,
                  decoration:
                      const InputDecoration(labelText: 'Requested description'),
                  maxLines: 3,
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: charsController,
                  decoration: const InputDecoration(
                    labelText:
                        'Requested characteristics (one per line: key: value)',
                  ),
                  maxLines: 7,
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: rawTextController,
                  decoration: const InputDecoration(
                    labelText:
                        'Texte brut fiche technique (optionnel, extraction auto)',
                  ),
                  maxLines: 6,
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, 'delete'),
            child: const Text('Supprimer'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, 'cancel'),
            child: const Text('Annuler'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(context, 'save'),
            child: const Text('Enregistrer'),
          ),
        ],
      ),
    );

    try {
      if (action == null || action == 'cancel') return;

      final name = nameController.text.trim();
      final unit = unitController.text.trim();
      final qtyText = qtyController.text.trim();
      final description = descController.text.trim();
      final characteristics = await _resolveRequestedCharacteristicsFromInputs(
        keyValueText: charsController.text.trim(),
        rawText: rawTextController.text.trim(),
      );

      final rows = <Map<String, dynamic>>[];
      for (final raw in _campaignItems) {
        if (raw is! Map<String, dynamic>) continue;
        final sameRow = campaignItemId != null
            ? raw['id'] == campaignItemId
            : raw['product_id'] == currentProductId;
        if (sameRow) continue;
        rows.add(_campaignItemToPayload(raw));
      }

      if (action == 'save') {
        if (name.isEmpty) {
          if (mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
              const SnackBar(
                  content: Text('Le nom du produit est obligatoire')),
            );
          }
          return;
        }

        var targetProductId = currentProductId;
        final currentName = (item['product_name']?.toString() ?? '').trim();
        final currentUnit = (product['unit']?.toString() ?? '').trim();
        final nameChanged = _nameKey(name) != _nameKey(currentName);
        final unitChanged = unit != currentUnit;

        if (nameChanged || unitChanged) {
          final existingProductId = _findProductIdByName(name);
          if (existingProductId != null) {
            targetProductId = existingProductId;
          } else {
            final created = await ProductsApi.create({
              'name': name,
              if (unit.isNotEmpty) 'unit': unit,
              if (description.isNotEmpty) 'description': description,
              if (characteristics != null && characteristics.isNotEmpty)
                'characteristics': characteristics,
            });
            targetProductId = created['id'] as int;
            _products = await ProductsApi.list();
          }
        }

        rows.add({
          'product_id': targetProductId,
          'requested_qty': qtyText.isEmpty ? null : double.tryParse(qtyText),
          'requested_description': description.isEmpty ? null : description,
          'requested_characteristics': characteristics,
          'requested_embedding_score': item['requested_embedding_score'],
        });
      }

      final dedupByProductId = <int, Map<String, dynamic>>{};
      for (final row in rows) {
        final pid = row['product_id'];
        if (pid is int) {
          dedupByProductId[pid] = row;
        }
      }
      final payload = dedupByProductId.values.toList();
      await CampaignsApi.setItems(campaignId, payload);
      await _loadCampaignItems();
      if (!mounted) return;
      setState(() {});
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(action == 'delete'
              ? 'Produit retire de la campagne'
              : 'Produit mis a jour'),
        ),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    } finally {
      nameController.dispose();
      unitController.dispose();
      qtyController.dispose();
      descController.dispose();
      charsController.dispose();
      rawTextController.dispose();
    }
  }

  Future<void> _openExtractedItemsFullscreen() async {
    await showDialog<void>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setStateDialog) => Dialog(
          insetPadding: const EdgeInsets.all(20),
          child: SizedBox(
            width: 1280,
            height: 820,
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                children: [
                  Row(
                    children: [
                      const Text(
                        'Produits extraits des PDF joints',
                        style: TextStyle(
                            fontSize: 18, fontWeight: FontWeight.w700),
                      ),
                      const Spacer(),
                      IconButton(
                        onPressed: () => Navigator.of(context).pop(),
                        icon: const Icon(Icons.close),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Expanded(
                    child: _buildExtractedItemsPane(
                      showExpandButton: false,
                      onThresholdChanged: () => setStateDialog(() {}),
                      namesOnly: false,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Future<void> _sendCampaign() async {
    if (_selectedCampaign == null) return;
    if (_isSelectedCampaignClosed) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Campagne cloturee: envoi bloque')),
        );
      }
      return;
    }
    final confirm = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Send campaign'),
        content: const Text('Send this campaign to selected recipients?'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          ElevatedButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Send')),
        ],
      ),
    );
    if (confirm != true) return;
    try {
      final result = await CampaignsApi.send(_selectedCampaign!['id'] as int);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
            content: Text(
                'Queued: ${result['queued']}, Skipped: ${result['skipped']}')),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  Future<void> _closeCampaign() async {
    if (_selectedCampaign == null) return;
    if (_isSelectedCampaignClosed) return;
    final confirm = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Cloturer la campagne'),
        content: const Text(
          'Cette action bloque les nouveaux envois. Continuer ?',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Annuler'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Cloturer'),
          ),
        ],
      ),
    );
    if (confirm != true) return;
    try {
      final updated = await CampaignsApi.close(_selectedCampaign!['id'] as int);
      setState(() {
        _selectedCampaign = updated;
      });
      await _load();
      await _selectCampaign(updated);
      if (!mounted) return;
      final status = (updated['status'] ?? '').toString();
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Campagne cloturee (status: $status)')),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  Future<void> _exportCampaignExcel() async {
    final campaign = _selectedCampaign;
    if (campaign == null) return;
    final campaignId = campaign['id'] as int;
    final title = (campaign['title'] ?? 'campaign_$campaignId').toString();
    try {
      dynamic bytes;
      var extension = 'xlsx';
      try {
        bytes = await CampaignsApi.exportXlsx(campaignId);
      } catch (err) {
        final message = err.toString();
        final match = RegExp(r'HTTP\s+(\d{3})').firstMatch(message);
        final statusCode =
            match == null ? null : int.tryParse(match.group(1) ?? '');
        if (statusCode != 404 && statusCode != 405) rethrow;
        bytes = await CampaignsApi.exportCsv(campaignId);
        extension = 'csv';
      }
      final fileName = '${title.replaceAll(' ', '_')}_$campaignId.$extension';
      final path = await FilePicker.platform.saveFile(
        dialogTitle: extension == 'xlsx'
            ? 'Enregistrer export Excel'
            : 'Backend ancien: enregistrer export CSV',
        fileName: fileName,
        type: FileType.custom,
        allowedExtensions: [extension],
      );
      if (path == null) return;
      final savePath = _ensureFileExtension(path, extension);
      await File(savePath).writeAsBytes(bytes, flush: true);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            extension == 'xlsx'
                ? 'Export Excel enregistre: $savePath'
                : 'Export CSV enregistre (compatibilite): $savePath',
          ),
        ),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  Future<void> _purgeCampaign() async {
    final campaign = _selectedCampaign;
    if (campaign == null) return;
    if (!_isSelectedCampaignClosed) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Cloture la campagne avant purge')),
        );
      }
      return;
    }

    final confirm = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Purger la campagne'),
        content: const Text(
          'Cette action supprime les donnees de campagne (irreversible). '
          'Exporte le fichier Excel avant de continuer.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Annuler'),
          ),
          ElevatedButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Purger'),
          ),
        ],
      ),
    );
    if (confirm != true) return;

    try {
      final campaignId = campaign['id'] as int;
      final result = await CampaignsApi.purge(campaignId);
      await _load();
      if (!mounted) return;
      final files = result['files'] as Map<String, dynamic>?;
      final removed = (files?['removed'] ?? 0).toString();
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('Campagne purgee. Fichiers supprimes: $removed'),
        ),
      );
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  Widget _buildOcrLoadingOverlay() {
    final seconds = _ocrStartedAt == null
        ? null
        : DateTime.now().difference(_ocrStartedAt!).inSeconds;
    final progress = _ocrProgress.clamp(0.0, 1.0).toDouble();
    final hasDeterminateProgress = progress > 0.0 && progress <= 1.0;

    return Positioned.fill(
      child: Stack(
        children: [
          const ModalBarrier(
            dismissible: false,
            color: Colors.black45,
          ),
          Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 560),
              child: Card(
                elevation: 12,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(14),
                ),
                child: Padding(
                  padding: const EdgeInsets.all(18),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Row(
                        children: [
                          SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2.5),
                          ),
                          SizedBox(width: 10),
                          Text(
                            'Analyse des fichiers en cours',
                            style: TextStyle(
                              fontSize: 16,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 10),
                      Text(
                        _ocrStatus.trim().isEmpty
                            ? 'Analyse des fichiers en cours...'
                            : _ocrStatus,
                        style: const TextStyle(fontSize: 13),
                      ),
                      const SizedBox(height: 12),
                      LinearProgressIndicator(
                        value: hasDeterminateProgress ? progress : null,
                      ),
                      const SizedBox(height: 8),
                      Text(
                        hasDeterminateProgress
                            ? 'Progression: ${(progress * 100).toStringAsFixed(0)}%'
                            : 'Progression: en cours...',
                        style: const TextStyle(
                          fontSize: 12,
                          color: Colors.black54,
                        ),
                      ),
                      if (seconds != null) ...[
                        const SizedBox(height: 4),
                        Text(
                          'Temps ecoule: ${seconds}s',
                          style: const TextStyle(
                            fontSize: 12,
                            color: Colors.black54,
                          ),
                        ),
                      ],
                      const SizedBox(height: 6),
                      const Text(
                        'Patientez, le traitement peut prendre un moment selon le nombre de PDFs.',
                        style: TextStyle(fontSize: 12, color: Colors.black54),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
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
    final campaigns = _filteredCampaigns;
    return Stack(
      children: [
        Padding(
          padding: const EdgeInsets.all(24),
          child: LayoutBuilder(
            builder: (context, constraints) {
              final compact = constraints.maxWidth < 1180;
              final sidebarHeight =
                  (constraints.maxHeight * 0.34).clamp(300.0, 420.0);
              if (compact) {
                return Column(
                  children: [
                    SizedBox(
                      height: sidebarHeight,
                      child: _buildCampaignsSidebar(
                        campaigns: campaigns,
                        compact: true,
                      ),
                    ),
                    const SizedBox(height: 16),
                    Expanded(child: _buildCampaignDetailPanel()),
                  ],
                );
              }
              return Row(
                children: [
                  _buildCampaignsSidebar(
                    campaigns: campaigns,
                    compact: false,
                  ),
                  const SizedBox(width: 24),
                  Expanded(child: _buildCampaignDetailPanel()),
                ],
              );
            },
          ),
        ),
        if (_ocrAnalyzing) _buildOcrLoadingOverlay(),
      ],
    );
  }
}
