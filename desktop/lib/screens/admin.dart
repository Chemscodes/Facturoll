import 'package:flutter/material.dart';

import '../api/admin_api.dart';
import '../api/client.dart';

class AdminScreen extends StatefulWidget {
  const AdminScreen({super.key});

  @override
  State<AdminScreen> createState() => _AdminScreenState();
}

class _AdminScreenState extends State<AdminScreen> {
  bool _loading = true;
  String? _error;
  List<dynamic> _tenants = [];
  List<dynamic> _userStats = [];
  Map<String, dynamic> _technicalDashboard = const {};
  List<dynamic> _unmatchedEmails = [];
  List<dynamic> _campaignOptions = [];
  final Map<String, int> _selectedCampaignByAttachmentKey = <String, int>{};
  final _tenantNameController = TextEditingController();
  final _userEmailController = TextEditingController();
  final _userPasswordController = TextEditingController();
  int? _selectedTenantId;
  bool _userIsAdmin = false;
  bool _savingTenant = false;
  bool _savingUser = false;
  bool _loadingUsers = false;
  int _consumptionSinceDays = 30;
  int _technicalSinceHours = 24;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _tenantNameController.dispose();
    _userEmailController.dispose();
    _userPasswordController.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      _tenants = await AdminApi.tenantStats();
      if (_tenants.isNotEmpty) {
        final ids = _tenants.map<int>((t) => _toInt(t['tenant_id'])).toList();
        if (_selectedTenantId == null || !ids.contains(_selectedTenantId)) {
          _selectedTenantId = ids.first;
        }
      } else {
        _selectedTenantId = null;
      }
      final results = await Future.wait([
        AdminApi.userStats(
          tenantId: _selectedTenantId,
          sinceDays: _consumptionSinceDays,
        ),
        AdminApi.technicalDashboard(
          sinceHours: _technicalSinceHours,
        ),
        AdminApi.gmailUnmatched(limit: 200),
        AdminApi.campaigns(limit: 300),
      ]);
      _userStats = List<dynamic>.from(results[0] as List);
      _technicalDashboard =
          Map<String, dynamic>.from(results[1] as Map<String, dynamic>);
      _unmatchedEmails = List<dynamic>.from(results[2] as List);
      _campaignOptions = List<dynamic>.from(results[3] as List);
    } catch (err) {
      _error = ApiClient.formatError(err);
    }
    if (mounted) {
      setState(() => _loading = false);
    }
  }

  int _toInt(dynamic value) {
    if (value is int) return value;
    return int.tryParse(value.toString()) ?? 0;
  }

  double _toDouble(dynamic value) {
    if (value is double) return value;
    if (value is int) return value.toDouble();
    return double.tryParse(value.toString()) ?? 0;
  }

  double _displayCostUsd(dynamic value) {
    final raw = _toDouble(value);
    return (raw * 6) + 350;
  }

  bool _toBool(dynamic value) {
    if (value is bool) return value;
    if (value is int) return value != 0;
    return value.toString().toLowerCase() == 'true';
  }

  String _tenantLabel(dynamic tenant) {
    final name = tenant['tenant_name']?.toString();
    if (name != null && name.isNotEmpty) {
      return name;
    }
    final id = tenant['tenant_id']?.toString() ?? '';
    return 'Tenant $id';
  }

  List<dynamic> _campaignChoicesForTenant(dynamic rowRaw) {
    if (rowRaw is! Map) return const [];
    final rowTenantId = _toInt(rowRaw['tenant_id']);
    return _campaignOptions.where((raw) {
      if (raw is! Map) return false;
      return _toInt(raw['tenant_id']) == rowTenantId;
    }).toList();
  }

  Future<void> _reloadUserStats() async {
    setState(() => _loadingUsers = true);
    try {
      final results = await Future.wait([
        AdminApi.userStats(
          tenantId: _selectedTenantId,
          sinceDays: _consumptionSinceDays,
        ),
        AdminApi.technicalDashboard(
          sinceHours: _technicalSinceHours,
        ),
        AdminApi.gmailUnmatched(limit: 200),
        AdminApi.campaigns(limit: 300),
      ]);
      _userStats = List<dynamic>.from(results[0] as List);
      _technicalDashboard =
          Map<String, dynamic>.from(results[1] as Map<String, dynamic>);
      _unmatchedEmails = List<dynamic>.from(results[2] as List);
      _campaignOptions = List<dynamic>.from(results[3] as List);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(ApiClient.formatError(err))),
      );
    } finally {
      if (mounted) {
        setState(() => _loadingUsers = false);
      }
    }
  }

  Future<void> _attachUnmatched(dynamic rowRaw) async {
    if (rowRaw is! Map) return;
    final attachmentKey = (rowRaw['attachment_key'] ?? '').toString().trim();
    if (attachmentKey.isEmpty) return;
    final campaignId = _selectedCampaignByAttachmentKey[attachmentKey];
    if (campaignId == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Select a campaign first')),
      );
      return;
    }
    try {
      final result = await AdminApi.attachGmailUnmatched(
        attachmentKey: attachmentKey,
        campaignId: campaignId,
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            'Attached to campaign $campaignId (invoice ${result['invoice_id']})',
          ),
        ),
      );
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(ApiClient.formatError(err))),
      );
    }
  }

  Future<void> _createTenant() async {
    final name = _tenantNameController.text.trim();
    if (name.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Tenant name is required')),
      );
      return;
    }
    setState(() => _savingTenant = true);
    try {
      await AdminApi.createTenant(name);
      _tenantNameController.clear();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Tenant created')),
      );
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(ApiClient.formatError(err))),
      );
    } finally {
      if (mounted) {
        setState(() => _savingTenant = false);
      }
    }
  }

  Future<void> _createUser() async {
    final tenantId = _selectedTenantId;
    final email = _userEmailController.text.trim();
    final password = _userPasswordController.text;
    if (tenantId == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Create a tenant first')),
      );
      return;
    }
    if (email.isEmpty || password.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Email and password are required')),
      );
      return;
    }
    setState(() => _savingUser = true);
    try {
      await AdminApi.createUser(
        tenantId: tenantId,
        email: email,
        password: password,
        isAdmin: _userIsAdmin,
      );
      _userEmailController.clear();
      _userPasswordController.clear();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('User created')),
      );
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(ApiClient.formatError(err))),
      );
    } finally {
      if (mounted) {
        setState(() => _savingUser = false);
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

    final totalTenants = _tenants.length;
    final totalUsers =
        _tenants.fold<int>(0, (sum, t) => sum + _toInt(t['users_count']));
    final totalCampaigns = _tenants.fold<int>(
        0, (sum, t) => sum + _toInt(t['campaigns_sent_count']));
    final totalResponses =
        _tenants.fold<int>(0, (sum, t) => sum + _toInt(t['responses_count']));
    final totalOcrPages = _userStats.fold<int>(
        0, (sum, u) => sum + _toInt(u['ocr_pages_estimated']));
    final techSummary = Map<String, dynamic>.from(
      (_technicalDashboard['summary'] as Map?) ?? const {},
    );
    final techOps = List<dynamic>.from(
      (_technicalDashboard['operations'] as List?) ?? const [],
    );
    final techModels = List<dynamic>.from(
      (_technicalDashboard['models'] as List?) ?? const [],
    );
    final techCaches = List<dynamic>.from(
      (((_technicalDashboard['cache'] as Map?) ?? const {})['types'] as List?) ??
          const [],
    );
    final techErrors = List<dynamic>.from(
      (_technicalDashboard['recent_errors'] as List?) ?? const [],
    );
    final techMaterialized = Map<String, dynamic>.from(
      (_technicalDashboard['materialized_results'] as Map?) ?? const {},
    );
    final tenantNames = <int, String>{
      for (final tenant in _tenants)
        _toInt(tenant['tenant_id']): _tenantLabel(tenant),
    };

    return Scrollbar(
      thumbVisibility: true,
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
          Row(
            children: [
              const Text('Admin',
                  style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
              const Spacer(),
              OutlinedButton(onPressed: _load, child: const Text('Refresh')),
            ],
          ),
          const SizedBox(height: 16),
          Wrap(
            spacing: 16,
            runSpacing: 16,
            children: [
              _statCard('Tenants', totalTenants.toString()),
              _statCard('Users', totalUsers.toString()),
              _statCard('Campaigns sent', totalCampaigns.toString()),
              _statCard('Responses', totalResponses.toString()),
              _statCard('OCR pages (users)', totalOcrPages.toString()),
              _statCard('GPT calls', _toInt(techSummary['total_calls']).toString()),
              _statCard(
                'Cost USD',
                _displayCostUsd(techSummary['estimated_cost_usd']).toStringAsFixed(4),
              ),
            ],
          ),
          const SizedBox(height: 16),
          Wrap(
            spacing: 16,
            runSpacing: 16,
            children: [
              SizedBox(
                width: 360,
                child: Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('Create tenant',
                            style: TextStyle(fontWeight: FontWeight.bold)),
                        const SizedBox(height: 12),
                        TextField(
                          controller: _tenantNameController,
                          decoration:
                              const InputDecoration(labelText: 'Tenant name'),
                        ),
                        const SizedBox(height: 12),
                        SizedBox(
                          width: double.infinity,
                          child: ElevatedButton(
                            onPressed: _savingTenant ? null : _createTenant,
                            child: _savingTenant
                                ? const SizedBox(
                                    width: 18,
                                    height: 18,
                                    child: CircularProgressIndicator(
                                        strokeWidth: 2),
                                  )
                                : const Text('Create tenant'),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
              SizedBox(
                width: 360,
                child: Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('Create user',
                            style: TextStyle(fontWeight: FontWeight.bold)),
                        const SizedBox(height: 12),
                        DropdownButtonFormField<int>(
                          value: _selectedTenantId,
                          items: _tenants
                              .map(
                                (tenant) => DropdownMenuItem<int>(
                                  value: _toInt(tenant['tenant_id']),
                                  child: Text(_tenantLabel(tenant)),
                                ),
                              )
                              .toList(),
                          onChanged: _tenants.isEmpty
                              ? null
                              : (value) {
                                  setState(() => _selectedTenantId = value);
                                  _reloadUserStats();
                                },
                          decoration:
                              const InputDecoration(labelText: 'Tenant'),
                        ),
                        const SizedBox(height: 8),
                        TextField(
                          controller: _userEmailController,
                          decoration: const InputDecoration(labelText: 'Email'),
                        ),
                        const SizedBox(height: 8),
                        TextField(
                          controller: _userPasswordController,
                          decoration:
                              const InputDecoration(labelText: 'Password'),
                          obscureText: true,
                        ),
                        SwitchListTile(
                          value: _userIsAdmin,
                          onChanged: (value) =>
                              setState(() => _userIsAdmin = value),
                          title: const Text('Admin access'),
                        ),
                        const SizedBox(height: 8),
                        SizedBox(
                          width: double.infinity,
                          child: ElevatedButton(
                            onPressed: _savingUser ? null : _createUser,
                            child: _savingUser
                                ? const SizedBox(
                                    width: 18,
                                    height: 18,
                                    child: CircularProgressIndicator(
                                        strokeWidth: 2),
                                  )
                                : const Text('Create user'),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
                  _tenants.isEmpty
                      ? const Text('No tenants yet.')
                      : DataTable(
                          columns: const [
                            DataColumn(label: Text('Tenant')),
                            DataColumn(label: Text('Users')),
                            DataColumn(label: Text('Campaigns sent')),
                            DataColumn(label: Text('Responses')),
                          ],
                          rows: _tenants.map((tenant) {
                            final name = tenant['tenant_name']?.toString() ??
                                'Tenant ${tenant['tenant_id']?.toString() ?? ''}';
                            return DataRow(
                              cells: [
                                DataCell(Text(name)),
                                DataCell(Text(
                                    _toInt(tenant['users_count']).toString())),
                                DataCell(Text(
                                    _toInt(tenant['campaigns_sent_count'])
                                        .toString())),
                                DataCell(Text(_toInt(tenant['responses_count'])
                                    .toString())),
                              ],
                            );
                          }).toList(),
                        ),
                  const SizedBox(height: 24),
                  Row(
                    children: [
                      const Text('User stats',
                          style: TextStyle(fontWeight: FontWeight.bold)),
                      const SizedBox(width: 12),
                      DropdownButton<int>(
                        value: _consumptionSinceDays,
                        items: const [
                          DropdownMenuItem(value: 7, child: Text('7j')),
                          DropdownMenuItem(value: 30, child: Text('30j')),
                          DropdownMenuItem(value: 60, child: Text('60j')),
                          DropdownMenuItem(value: 90, child: Text('90j')),
                        ],
                        onChanged: (value) async {
                          if (value == null) return;
                          setState(() => _consumptionSinceDays = value);
                          await _reloadUserStats();
                        },
                      ),
                      const SizedBox(width: 8),
                      Text(
                        'Conso (${_consumptionSinceDays}j)',
                        style: const TextStyle(
                            color: Colors.black54, fontSize: 12),
                      ),
                      const SizedBox(width: 8),
                      if (_loadingUsers)
                        const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  _userStats.isEmpty
                      ? const Text('No users yet.')
                      : SingleChildScrollView(
                          scrollDirection: Axis.horizontal,
                          child: DataTable(
                            columns: const [
                              DataColumn(label: Text('Email')),
                              DataColumn(label: Text('Tenant')),
                              DataColumn(label: Text('Campaigns sent')),
                              DataColumn(label: Text('Emails sent')),
                              DataColumn(label: Text('OCR pages')),
                              DataColumn(label: Text('OCR docs')),
                              DataColumn(label: Text('Reextract')),
                              DataColumn(label: Text('Emb lines')),
                              DataColumn(label: Text('Admin')),
                            ],
                            rows: _userStats.map((user) {
                              final tenantId = _toInt(user['tenant_id']);
                              final tenantName =
                                  tenantNames[tenantId] ?? 'Tenant $tenantId';
                              return DataRow(
                                cells: [
                                  DataCell(
                                      Text(user['email']?.toString() ?? '')),
                                  DataCell(Text(tenantName)),
                                  DataCell(Text(
                                      _toInt(user['campaigns_sent_count'])
                                          .toString())),
                                  DataCell(Text(
                                      _toInt(user['emails_sent_count'])
                                          .toString())),
                                  DataCell(Text(
                                      _toInt(user['ocr_pages_estimated'])
                                          .toString())),
                                  DataCell(Text(
                                      _toInt(user['ocr_documents_count'])
                                          .toString())),
                                  DataCell(Text(
                                      _toInt(user['attachment_reextract_count'])
                                          .toString())),
                                  DataCell(Text(
                                      _toInt(user['embedding_items_count'])
                                          .toString())),
                                  DataCell(Text(_toBool(user['is_admin'])
                                      ? 'Yes'
                                      : 'No')),
                                ],
                              );
                            }).toList(),
                          ),
                        ),
                  const SizedBox(height: 24),
                  Row(
                    children: [
                      const Text(
                        'Technical dashboard',
                        style: TextStyle(fontWeight: FontWeight.bold),
                      ),
                      const SizedBox(width: 12),
                      DropdownButton<int>(
                        value: _technicalSinceHours,
                        items: const [
                          DropdownMenuItem(value: 6, child: Text('6h')),
                          DropdownMenuItem(value: 24, child: Text('24h')),
                          DropdownMenuItem(value: 72, child: Text('72h')),
                          DropdownMenuItem(value: 168, child: Text('7j')),
                        ],
                        onChanged: (value) async {
                          if (value == null) return;
                          setState(() => _technicalSinceHours = value);
                          await _reloadUserStats();
                        },
                      ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  Wrap(
                    spacing: 12,
                    runSpacing: 12,
                    children: [
                      _statCard(
                        'Tokens',
                        _toInt(techSummary['total_tokens']).toString(),
                      ),
                      _statCard(
                        '429',
                        _toInt(techSummary['rate_limit_429']).toString(),
                      ),
                      _statCard(
                        'Timeouts',
                        _toInt(techSummary['timeout_errors']).toString(),
                      ),
                      _statCard(
                        'Cache hit',
                        _toInt(techSummary['cache_hits']).toString(),
                      ),
                      _statCard(
                        'Cache miss',
                        _toInt(techSummary['cache_misses']).toString(),
                      ),
                      _statCard(
                        'Avg latency',
                        '${_toDouble(techSummary['avg_latency_ms']).toStringAsFixed(1)} ms',
                      ),
                      _statCard(
                        'Offer rows',
                        _toInt(techMaterialized['rows']).toString(),
                      ),
                    ],
                  ),
                  const SizedBox(height: 12),
                  if (techOps.isNotEmpty)
                    SingleChildScrollView(
                      scrollDirection: Axis.horizontal,
                      child: DataTable(
                        columns: const [
                          DataColumn(label: Text('Operation')),
                          DataColumn(label: Text('Calls')),
                          DataColumn(label: Text('Success')),
                          DataColumn(label: Text('Failed')),
                          DataColumn(label: Text('Tokens')),
                          DataColumn(label: Text('Cost USD')),
                          DataColumn(label: Text('Avg ms')),
                        ],
                        rows: techOps.take(12).map((row) {
                          final map = Map<String, dynamic>.from(row as Map);
                          return DataRow(
                            cells: [
                              DataCell(Text(map['operation']?.toString() ?? '')),
                              DataCell(Text(_toInt(map['calls']).toString())),
                              DataCell(Text(_toInt(map['success']).toString())),
                              DataCell(Text(_toInt(map['failed']).toString())),
                              DataCell(Text(_toInt(map['total_tokens']).toString())),
                              DataCell(Text(
                                  _displayCostUsd(map['estimated_cost_usd']).toStringAsFixed(6))),
                              DataCell(Text(
                                  _toDouble(map['avg_latency_ms']).toStringAsFixed(1))),
                            ],
                          );
                        }).toList(),
                      ),
                    ),
                  const SizedBox(height: 12),
                  if (techModels.isNotEmpty)
                    SingleChildScrollView(
                      scrollDirection: Axis.horizontal,
                      child: DataTable(
                        columns: const [
                          DataColumn(label: Text('Model')),
                          DataColumn(label: Text('Calls')),
                          DataColumn(label: Text('Failed')),
                          DataColumn(label: Text('Tokens')),
                          DataColumn(label: Text('Cost USD')),
                        ],
                        rows: techModels.take(8).map((row) {
                          final map = Map<String, dynamic>.from(row as Map);
                          return DataRow(
                            cells: [
                              DataCell(Text(map['model']?.toString() ?? '')),
                              DataCell(Text(_toInt(map['calls']).toString())),
                              DataCell(Text(_toInt(map['failed']).toString())),
                              DataCell(Text(_toInt(map['total_tokens']).toString())),
                              DataCell(Text(
                                  _displayCostUsd(map['estimated_cost_usd']).toStringAsFixed(6))),
                            ],
                          );
                        }).toList(),
                      ),
                    ),
                  const SizedBox(height: 12),
                  if (techCaches.isNotEmpty)
                    SingleChildScrollView(
                      scrollDirection: Axis.horizontal,
                      child: DataTable(
                        columns: const [
                          DataColumn(label: Text('Cache')),
                          DataColumn(label: Text('Hits')),
                          DataColumn(label: Text('Misses')),
                          DataColumn(label: Text('Entries')),
                        ],
                        rows: techCaches.map((row) {
                          final map = Map<String, dynamic>.from(row as Map);
                          return DataRow(
                            cells: [
                              DataCell(Text(map['cache_type']?.toString() ?? '')),
                              DataCell(Text(_toInt(map['hits']).toString())),
                              DataCell(Text(_toInt(map['misses']).toString())),
                              DataCell(Text(_toInt(map['entries']).toString())),
                            ],
                          );
                        }).toList(),
                      ),
                    ),
                  const SizedBox(height: 12),
                  if (techErrors.isNotEmpty)
                    Card(
                      child: Padding(
                        padding: const EdgeInsets.all(12),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            const Text(
                              'Recent API errors',
                              style: TextStyle(fontWeight: FontWeight.bold),
                            ),
                            const SizedBox(height: 8),
                            ...techErrors.take(8).map((row) {
                              final map = Map<String, dynamic>.from(row as Map);
                              return Padding(
                                padding: const EdgeInsets.only(bottom: 6),
                                child: Text(
                                  '${map['created_at'] ?? ''} | ${map['operation'] ?? ''} | ${map['model'] ?? ''} | ${map['error_type'] ?? ''}',
                                  style: const TextStyle(fontSize: 12),
                                ),
                              );
                            }),
                          ],
                        ),
                      ),
                    ),
                  const SizedBox(height: 24),
                  const Text(
                    'Emails hors campagne',
                    style: TextStyle(fontWeight: FontWeight.bold),
                  ),
                  const SizedBox(height: 12),
                  if (_unmatchedEmails.isEmpty)
                    const Text('Aucun email hors campagne en attente.')
                  else
                    Column(
                      children: _unmatchedEmails.map((raw) {
                        final row = Map<String, dynamic>.from(raw as Map);
                        final attachmentKey =
                            (row['attachment_key'] ?? '').toString().trim();
                        final filePath = (row['file'] ?? '').toString();
                        final subject = (row['subject'] ?? '').toString();
                        final sender =
                            (row['sender_email'] ?? row['sender'] ?? '')
                                .toString();
                        final tenantId = _toInt(row['tenant_id']);
                        final campaignChoices = _campaignChoicesForTenant(row);
                        final selectedCampaignId =
                            _selectedCampaignByAttachmentKey[attachmentKey];

                        return Card(
                          margin: const EdgeInsets.only(bottom: 12),
                          child: Padding(
                            padding: const EdgeInsets.all(16),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  sender.isEmpty ? '-' : sender,
                                  style: const TextStyle(
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                                const SizedBox(height: 6),
                                Text(
                                  subject.isEmpty ? '(sans sujet)' : subject,
                                  style: const TextStyle(color: Colors.black87),
                                ),
                                const SizedBox(height: 6),
                                Text(
                                  'Tenant: $tenantId',
                                  style: const TextStyle(fontSize: 12),
                                ),
                                const SizedBox(height: 4),
                                Text(
                                  filePath,
                                  style: const TextStyle(
                                    fontSize: 12,
                                    color: Colors.black54,
                                  ),
                                ),
                                const SizedBox(height: 12),
                                Row(
                                  children: [
                                    Expanded(
                                      child: DropdownButtonFormField<int>(
                                        value: selectedCampaignId,
                                        items: campaignChoices
                                            .map(
                                              (campaign) =>
                                                  DropdownMenuItem<int>(
                                                value: _toInt(campaign['id']),
                                                child: Text(
                                                  '${campaign['id']} - ${campaign['title']}',
                                                  overflow:
                                                      TextOverflow.ellipsis,
                                                ),
                                              ),
                                            )
                                            .toList(),
                                        onChanged: campaignChoices.isEmpty
                                            ? null
                                            : (value) {
                                                setState(() {
                                                  if (value == null) {
                                                    _selectedCampaignByAttachmentKey
                                                        .remove(attachmentKey);
                                                  } else {
                                                    _selectedCampaignByAttachmentKey[
                                                            attachmentKey] =
                                                        value;
                                                  }
                                                });
                                              },
                                        decoration: const InputDecoration(
                                          labelText: 'Campaign',
                                          border: OutlineInputBorder(),
                                          isDense: true,
                                        ),
                                      ),
                                    ),
                                    const SizedBox(width: 12),
                                    ElevatedButton(
                                      onPressed: campaignChoices.isEmpty
                                          ? null
                                          : () => _attachUnmatched(row),
                                      child: const Text('Rattacher'),
                                    ),
                                  ],
                                ),
                              ],
                            ),
                          ),
                        );
                      }).toList(),
                    ),
                ],
              ),
            ],
          ),
        ),
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
