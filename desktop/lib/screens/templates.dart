import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/templates_api.dart';
import '../widgets/empty_state.dart';

class TemplatesScreen extends StatefulWidget {
  const TemplatesScreen({super.key});

  @override
  State<TemplatesScreen> createState() => _TemplatesScreenState();
}

class _TemplatesScreenState extends State<TemplatesScreen> {
  bool _loading = true;
  String? _error;
  List<dynamic> _templates = [];
  String _searchQuery = '';
  final _searchController = TextEditingController();

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

  void _showError(Object err) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      _templates = await TemplatesApi.list();
    } catch (err) {
      _error = ApiClient.formatError(err);
    }
    if (mounted) {
      setState(() => _loading = false);
    }
  }

  List<dynamic> get _filteredTemplates {
    final query = _searchQuery.trim().toLowerCase();
    if (query.isEmpty) return _templates;
    return _templates.where((t) {
      final name = (t['name'] ?? '').toString().toLowerCase();
      final subject = (t['subject'] ?? '').toString().toLowerCase();
      return name.contains(query) || subject.contains(query);
    }).toList();
  }

  Future<void> _showTemplateDialog({Map<String, dynamic>? template}) async {
    final nameController = TextEditingController(text: template?['name'] ?? '');
    final subjectController = TextEditingController(text: template?['subject'] ?? '');
    final bodyController = TextEditingController(text: template?['body_html'] ?? '');

    final result = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(template == null ? 'New Template' : 'Edit Template'),
        content: SizedBox(
          width: 520,
          child: SingleChildScrollView(
            child: Column(
              children: [
                TextField(controller: nameController, decoration: const InputDecoration(labelText: 'Name')),
                TextField(
                  controller: subjectController,
                  decoration: const InputDecoration(labelText: 'Email subject'),
                ),
                TextField(
                  controller: bodyController,
                  decoration: const InputDecoration(labelText: 'Email message (HTML)'),
                  maxLines: 8,
                ),
                const SizedBox(height: 8),
                const Align(
                  alignment: Alignment.centerLeft,
                  child: Text(
                    'Available variables: {{supplier_name}}, {{supplier_email}}, {{campaign_title}}, {{campaign_subject}}, {{reply_url}}',
                    style: TextStyle(fontSize: 12, color: Colors.grey),
                  ),
                ),
              ],
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          ElevatedButton(onPressed: () => Navigator.pop(context, true), child: const Text('Save')),
        ],
      ),
    );

    if (result == true) {
      final payload = {
        'name': nameController.text.trim(),
        'subject': subjectController.text.trim(),
        'body_html': bodyController.text.trim(),
      };
      try {
        if (template == null) {
          await TemplatesApi.create(payload);
        } else {
          await TemplatesApi.update(template['id'] as int, payload);
        }
        await _load();
      } catch (err) {
        if (mounted) {
          _showError(err);
        }
      }
    }
  }

  Future<void> _deleteTemplate(Map<String, dynamic> template) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete Template'),
        content: const Text('Delete this template?'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          ElevatedButton(onPressed: () => Navigator.pop(context, true), child: const Text('Delete')),
        ],
      ),
    );
    if (result == true) {
      try {
        await TemplatesApi.remove(template['id'] as int);
        await _load();
      } catch (err) {
        if (mounted) {
          _showError(err);
        }
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
    final templates = _filteredTemplates;
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Text('Email Templates', style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
              const Spacer(),
              ElevatedButton(onPressed: () => _showTemplateDialog(), child: const Text('New')),
              const SizedBox(width: 8),
              OutlinedButton(onPressed: _load, child: const Text('Refresh')),
            ],
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: 320,
            child: TextField(
              controller: _searchController,
              decoration: InputDecoration(
                labelText: 'Search templates',
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
          ),
          const SizedBox(height: 12),
          Expanded(
            child: templates.isEmpty
                ? EmptyState(
                    icon: Icons.description_outlined,
                    title: _templates.isEmpty ? 'No templates yet' : 'No templates match search',
                    message: _templates.isEmpty
                        ? 'Create a template to reuse email content quickly.'
                        : 'Try another keyword.',
                    action: _templates.isEmpty
                        ? ElevatedButton(
                            onPressed: () => _showTemplateDialog(),
                            child: const Text('Create template'),
                          )
                        : null,
                  )
                : SingleChildScrollView(
                    child: DataTable(
                      columns: const [
                        DataColumn(label: Text('Name')),
                        DataColumn(label: Text('Subject')),
                        DataColumn(label: Text('Actions')),
                      ],
                      rows: templates
                          .map(
                            (t) => DataRow(cells: [
                              DataCell(Text(t['name'] as String)),
                              DataCell(Text(t['subject'] as String)),
                              DataCell(Row(
                                children: [
                                  IconButton(
                                    icon: const Icon(Icons.edit),
                                    onPressed: () => _showTemplateDialog(template: t as Map<String, dynamic>),
                                  ),
                                  IconButton(
                                    icon: const Icon(Icons.delete),
                                    onPressed: () => _deleteTemplate(t as Map<String, dynamic>),
                                  ),
                                ],
                              )),
                            ]),
                          )
                          .toList(),
                    ),
                  ),
          ),
        ],
      ),
    );
  }
}
