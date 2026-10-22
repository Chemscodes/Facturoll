import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/campaigns_api.dart';
import '../widgets/empty_state.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  bool _loading = true;
  bool _campaignLoading = false;
  String? _error;
  List<dynamic> _campaigns = [];
  int? _selectedCampaignId;
  Map<String, dynamic>? _stats;
  DateTime? _lastRefreshAt;

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
      final campaigns = await CampaignsApi.list();
      _campaigns = campaigns;
      if (_campaigns.isNotEmpty && _selectedCampaignId == null) {
        _selectedCampaignId = _campaigns.first['id'] as int;
      }
      _lastRefreshAt = DateTime.now();
    } catch (err) {
      _error = ApiClient.formatError(err);
    }
    if (mounted) {
      setState(() => _loading = false);
    }
    if (_selectedCampaignId != null) {
      unawaited(_loadCampaignData());
    }
  }

  Future<void> _loadCampaignData() async {
    if (_selectedCampaignId == null) {
      if (!mounted) return;
      setState(() => _stats = null);
      return;
    }
    final campaignId = _selectedCampaignId!;
    if (mounted) {
      setState(() => _campaignLoading = true);
    }
    try {
      final stats = await CampaignsApi.stats(campaignId);
      if (!mounted || _selectedCampaignId != campaignId) return;
      setState(() => _stats = stats);
    } catch (err) {
      if (!mounted) return;
      setState(() => _error = ApiClient.formatError(err));
    } finally {
      if (mounted) {
        setState(() => _campaignLoading = false);
      }
    }
  }

  int _toInt(dynamic value) {
    if (value is int) return value;
    if (value is num) return value.toInt();
    if (value is String) return int.tryParse(value) ?? 0;
    return 0;
  }

  double _toDouble(dynamic value) {
    if (value is double) return value;
    if (value is num) return value.toDouble();
    if (value is String) return double.tryParse(value) ?? 0.0;
    return 0.0;
  }

  String _formatPercent(double ratio) {
    final value = (ratio * 100).clamp(0, 100);
    return '${value.toStringAsFixed(1)}%';
  }

  String _formatUsd(dynamic value) {
    return '\$${_toDouble(value).toStringAsFixed(4)}';
  }

  Map<String, dynamic> _mapOf(dynamic value) {
    if (value is Map) {
      return value.map((k, v) => MapEntry(k.toString(), v));
    }
    return const <String, dynamic>{};
  }

  List<Map<String, dynamic>> _listOfMaps(dynamic value) {
    if (value is! List) return const <Map<String, dynamic>>[];
    final out = <Map<String, dynamic>>[];
    for (final row in value) {
      if (row is Map) {
        out.add(row.map((k, v) => MapEntry(k.toString(), v)));
      }
    }
    return out;
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return Center(child: Text('Error: $_error'));
    }
    return Padding(
      padding: const EdgeInsets.all(24),
      child: SingleChildScrollView(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Dashboard',
                style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            Text(
              _lastRefreshAt == null
                  ? 'No refresh yet'
                  : 'Last refresh: ${_lastRefreshAt!.toLocal()}',
              style: const TextStyle(fontSize: 12, color: Colors.grey),
            ),
            const SizedBox(height: 16),
            if (_campaigns.isEmpty)
              const EmptyState(
                icon: Icons.campaign_outlined,
                title: 'No campaigns yet',
                message: 'Create a campaign to start tracking real metrics.',
              ),
            if (_campaigns.isNotEmpty)
              Row(
                children: [
                  const Text('Campagne: '),
                  const SizedBox(width: 12),
                  DropdownButton<int>(
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
                      setState(() => _selectedCampaignId = value);
                      await _loadCampaignData();
                    },
                  ),
                  const SizedBox(width: 24),
                  ElevatedButton(
                    onPressed: _load,
                    child: const Text('Rafraichir'),
                  ),
                  if (_campaignLoading) ...[
                    const SizedBox(width: 12),
                    const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2)),
                  ],
                ],
              ),
            const SizedBox(height: 20),
            if (_stats != null)
              Wrap(
                spacing: 16,
                runSpacing: 16,
                children: [
                  _statCard('Fournisseurs cibles',
                      _toInt(_stats!['total_recipients']).toString()),
                  _statCard('Emails envoyes',
                      _toInt(_stats!['sent_count']).toString()),
                  _statCard('Fournisseurs repondus',
                      _toInt(_stats!['responses_count']).toString()),
                  _statCard('Sans reponse',
                      _toInt(_stats!['pending_responses_count']).toString()),
                  _statCard('Taux de reponse',
                      _formatPercent(_toDouble(_stats!['response_rate']))),
                  _statCard('Emails en echec',
                      _toInt(_stats!['failed_count']).toString()),
                ],
              ),
            const SizedBox(height: 24),
            if (_stats != null)
              Builder(
                builder: (_) {
                  final costs = _mapOf(_stats!['costs']);
                  if (costs.isEmpty) {
                    return const Text(
                      'Couts detailles indisponibles sur ce backend.',
                      style: TextStyle(fontSize: 12, color: Colors.orange),
                    );
                  }
                  final summary = _mapOf(costs['summary']);
                  final groups = _listOfMaps(costs['by_group']);
                  final models = _listOfMaps(costs['by_model']);
                  final operations = _listOfMaps(costs['by_operation']);

                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text(
                        'Couts campagne (detailles)',
                        style: TextStyle(
                            fontSize: 18, fontWeight: FontWeight.w700),
                      ),
                      const SizedBox(height: 8),
                      Text(
                        'Scope: ${summary['scope'] ?? '-'}',
                        style:
                            const TextStyle(fontSize: 12, color: Colors.grey),
                      ),
                      const SizedBox(height: 12),
                      Wrap(
                        spacing: 16,
                        runSpacing: 16,
                        children: [
                          _statCard('Cout total IA',
                              _formatUsd(summary['total_usd'])),
                          _statCard(
                              'Appels IA', _toInt(summary['calls']).toString()),
                          _statCard('Tokens prompt',
                              _toInt(summary['prompt_tokens']).toString()),
                          _statCard('Tokens output',
                              _toInt(summary['completion_tokens']).toString()),
                          _statCard('Calls attribues',
                              _toInt(summary['attributed_calls']).toString()),
                          _statCard('Calls fenetre',
                              _toInt(summary['window_calls']).toString()),
                        ],
                      ),
                      const SizedBox(height: 14),
                      if (groups.isNotEmpty) ...[
                        const Text(
                          'Breakdown par bloc',
                          style: TextStyle(
                              fontSize: 14, fontWeight: FontWeight.w600),
                        ),
                        const SizedBox(height: 8),
                        Wrap(
                          spacing: 12,
                          runSpacing: 12,
                          children: groups
                              .map(
                                (row) => _statCard(
                                  '${row['label'] ?? row['key'] ?? '-'}\n(${_toInt(row['calls'])} calls)',
                                  _formatUsd(row['total_usd']),
                                  width: 240,
                                ),
                              )
                              .toList(),
                        ),
                      ],
                      const SizedBox(height: 8),
                      ExpansionTile(
                        tilePadding: EdgeInsets.zero,
                        title: const Text('Detail par modele'),
                        children: models
                            .map(
                              (row) => ListTile(
                                dense: true,
                                title: Text(row['model']?.toString() ?? '-'),
                                subtitle: Text(
                                    '${_toInt(row['calls'])} calls | ${_toInt(row['total_tokens'])} tokens'),
                                trailing: Text(_formatUsd(row['total_usd'])),
                              ),
                            )
                            .toList(),
                      ),
                      ExpansionTile(
                        tilePadding: EdgeInsets.zero,
                        title: const Text('Detail par operation'),
                        children: operations
                            .take(12)
                            .map(
                              (row) => ListTile(
                                dense: true,
                                title:
                                    Text(row['operation']?.toString() ?? '-'),
                                subtitle: Text(
                                    '${_toInt(row['calls'])} calls | ${_toInt(row['total_tokens'])} tokens'),
                                trailing: Text(_formatUsd(row['total_usd'])),
                              ),
                            )
                            .toList(),
                      ),
                    ],
                  );
                },
              ),
            const SizedBox(height: 12),
            const Text(
              'Source: /campaigns/{id}/stats',
              style: TextStyle(fontSize: 12, color: Colors.grey),
            ),
          ],
        ),
      ),
    );
  }

  Widget _statCard(String label, String value, {double width = 190}) {
    return Card(
      child: SizedBox(
        width: width,
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
