import 'package:flutter/material.dart';

import '../api/client.dart';
import '../api/groups_api.dart';
import '../api/suppliers_api.dart';
import '../widgets/empty_state.dart';

class GroupsScreen extends StatefulWidget {
  const GroupsScreen({super.key});

  @override
  State<GroupsScreen> createState() => _GroupsScreenState();
}

class _GroupsScreenState extends State<GroupsScreen> {
  bool _loading = true;
  String? _error;
  List<dynamic> _groups = [];
  List<dynamic> _suppliers = [];
  int? _selectedGroupId;
  Set<int> _selectedSupplierIds = {};
  String _groupQuery = '';
  String _memberQuery = '';
  final _groupSearchController = TextEditingController();
  final _memberSearchController = TextEditingController();

  void _showError(Object err) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
  }

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _groupSearchController.dispose();
    _memberSearchController.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      _groups = await GroupsApi.list();
      _suppliers = await SuppliersApi.list();
      if (_groups.isNotEmpty) {
        _selectedGroupId ??= _groups.first['id'] as int;
        await _loadMembers();
      }
    } catch (err) {
      _error = ApiClient.formatError(err);
    }
    if (mounted) {
      setState(() => _loading = false);
    }
  }

  Future<void> _loadMembers() async {
    if (_selectedGroupId == null) return;
    try {
      final members = await GroupsApi.listMembers(_selectedGroupId!);
      setState(() {
        _selectedSupplierIds = members.map<int>((m) => m['supplier_id'] as int).toSet();
      });
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  List<dynamic> get _filteredGroups {
    final query = _groupQuery.trim().toLowerCase();
    if (query.isEmpty) {
      return _groups;
    }
    return _groups.where((g) {
      final name = (g['name'] ?? '').toString().toLowerCase();
      final desc = (g['description'] ?? '').toString().toLowerCase();
      return name.contains(query) || desc.contains(query);
    }).toList();
  }

  List<dynamic> get _filteredSuppliers {
    final query = _memberQuery.trim().toLowerCase();
    if (query.isEmpty) {
      return _suppliers;
    }
    return _suppliers.where((s) {
      final name = (s['name'] ?? '').toString().toLowerCase();
      final type = (s['supplier_type'] ?? '').toString().toLowerCase();
      return name.contains(query) || type.contains(query);
    }).toList();
  }

  Future<void> _showGroupDialog({Map<String, dynamic>? group}) async {
    final nameController = TextEditingController(text: group?['name'] ?? '');
    final descController = TextEditingController(text: group?['description'] ?? '');

    final result = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(group == null ? 'Add Group' : 'Edit Group'),
        content: SizedBox(
          width: 400,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(controller: nameController, decoration: const InputDecoration(labelText: 'Name')),
              TextField(controller: descController, decoration: const InputDecoration(labelText: 'Description')),
            ],
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
        'description': descController.text.trim(),
      };
      try {
        if (group == null) {
          await GroupsApi.create(payload);
        } else {
          await GroupsApi.update(group['id'] as int, payload);
        }
        await _load();
      } catch (err) {
        if (mounted) {
          _showError(err);
        }
      }
    }
  }

  Future<void> _deleteGroup(Map<String, dynamic> group) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete Group'),
        content: const Text('Delete this group?'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          ElevatedButton(onPressed: () => Navigator.pop(context, true), child: const Text('Delete')),
        ],
      ),
    );
    if (result == true) {
      try {
        await GroupsApi.remove(group['id'] as int);
        _selectedGroupId = null;
        await _load();
      } catch (err) {
        if (mounted) {
          _showError(err);
        }
      }
    }
  }

  Future<void> _saveMembers() async {
    if (_selectedGroupId == null) return;
    try {
      await GroupsApi.replaceMembers(_selectedGroupId!, _selectedSupplierIds.toList());
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Members saved')));
    } catch (err) {
      if (mounted) {
        _showError(err);
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
    final groups = _filteredGroups;
    final suppliers = _filteredSuppliers;
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Row(
        children: [
          SizedBox(
            width: 260,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    const Text('Groups', style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
                    const Spacer(),
                    IconButton(onPressed: () => _showGroupDialog(), icon: const Icon(Icons.add)),
                    IconButton(onPressed: _load, icon: const Icon(Icons.refresh)),
                  ],
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _groupSearchController,
                  decoration: InputDecoration(
                    labelText: 'Search groups',
                    prefixIcon: const Icon(Icons.search),
                    suffixIcon: _groupQuery.isEmpty
                        ? null
                        : IconButton(
                            icon: const Icon(Icons.clear),
                            onPressed: () {
                              _groupSearchController.clear();
                              setState(() => _groupQuery = '');
                            },
                          ),
                  ),
                  onChanged: (value) => setState(() => _groupQuery = value),
                ),
                const SizedBox(height: 12),
                Expanded(
                  child: groups.isEmpty
                      ? EmptyState(
                          icon: Icons.group_outlined,
                          title: _groups.isEmpty ? 'No groups yet' : 'No groups match search',
                          message: _groups.isEmpty
                              ? 'Create a group to organize suppliers.'
                              : 'Try another keyword.',
                          action: _groups.isEmpty
                              ? ElevatedButton(
                                  onPressed: () => _showGroupDialog(),
                                  child: const Text('Create group'),
                                )
                              : null,
                        )
                      : ListView.builder(
                          itemCount: groups.length,
                          itemBuilder: (context, index) {
                            final group = groups[index] as Map<String, dynamic>;
                            final selected = group['id'] == _selectedGroupId;
                            return ListTile(
                              selected: selected,
                              title: Text(group['name'] as String),
                              subtitle: Text(group['description'] ?? ''),
                              onTap: () async {
                                setState(() => _selectedGroupId = group['id'] as int);
                                await _loadMembers();
                              },
                              trailing: PopupMenuButton<String>(
                                onSelected: (value) {
                                  if (value == 'edit') {
                                    _showGroupDialog(group: group);
                                  } else if (value == 'delete') {
                                    _deleteGroup(group);
                                  }
                                },
                                itemBuilder: (context) => const [
                                  PopupMenuItem(value: 'edit', child: Text('Edit')),
                                  PopupMenuItem(value: 'delete', child: Text('Delete')),
                                ],
                              ),
                            );
                          },
                        ),
                ),
              ],
            ),
          ),
          const SizedBox(width: 24),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text('Membership', style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
                const SizedBox(height: 8),
                if (_selectedGroupId == null)
                  const Text('Select a group to manage members.')
                else
                  Expanded(
                    child: Column(
                      children: [
                        TextField(
                          controller: _memberSearchController,
                          decoration: InputDecoration(
                            labelText: 'Search suppliers',
                            prefixIcon: const Icon(Icons.search),
                            suffixIcon: _memberQuery.isEmpty
                                ? null
                                : IconButton(
                                    icon: const Icon(Icons.clear),
                                    onPressed: () {
                                      _memberSearchController.clear();
                                      setState(() => _memberQuery = '');
                                    },
                                  ),
                          ),
                          onChanged: (value) => setState(() => _memberQuery = value),
                        ),
                        const SizedBox(height: 8),
                        Expanded(
                          child: suppliers.isEmpty
                              ? EmptyState(
                                  icon: Icons.group_outlined,
                                  title: _suppliers.isEmpty
                                      ? 'No suppliers yet'
                                      : 'No suppliers match search',
                                  message: _suppliers.isEmpty
                                      ? 'Create suppliers first, then add them to groups.'
                                      : 'Try a different keyword.',
                                )
                              : ListView(
                                  children: suppliers.map((supplier) {
                                    final id = supplier['id'] as int;
                                    final checked = _selectedSupplierIds.contains(id);
                                    return CheckboxListTile(
                                      value: checked,
                                      onChanged: (value) {
                                        setState(() {
                                          if (value == true) {
                                            _selectedSupplierIds.add(id);
                                          } else {
                                            _selectedSupplierIds.remove(id);
                                          }
                                        });
                                      },
                                      title: Text(supplier['name'] as String),
                                      subtitle: Text(supplier['supplier_type'] as String),
                                    );
                                  }).toList(),
                                ),
                        ),
                      ],
                    ),
                  ),
                if (_selectedGroupId != null)
                  Align(
                    alignment: Alignment.centerRight,
                    child: ElevatedButton(onPressed: _saveMembers, child: const Text('Save members')),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
