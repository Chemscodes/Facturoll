import 'package:flutter/material.dart';
import 'package:file_picker/file_picker.dart';

import '../api/client.dart';
import '../api/suppliers_api.dart';
import '../widgets/empty_state.dart';

class SuppliersScreen extends StatefulWidget {
  const SuppliersScreen({super.key});

  @override
  State<SuppliersScreen> createState() => _SuppliersScreenState();
}

class _SuppliersScreenState extends State<SuppliersScreen> {
  bool _loading = true;
  String? _error;
  List<dynamic> _suppliers = [];
  String _typeFilter = 'ALL';
  String _categoryFilter = 'ALL';
  String _activeFilter = 'ALL';
  String _searchQuery = '';
  int _sortColumnIndex = 0;
  bool _sortAscending = true;
  final _searchController = TextEditingController();

  void _showError(Object err) {
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(ApiClient.formatError(err))));
  }

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
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      _suppliers = await SuppliersApi.list();
    } catch (err) {
      _error = ApiClient.formatError(err);
    }
    if (mounted) {
      setState(() => _loading = false);
    }
  }

  List<dynamic> get _filteredSuppliers {
    final query = _searchQuery.trim().toLowerCase();
    final filtered = _suppliers.where((s) {
      final matchesType =
          _typeFilter == 'ALL' || s['supplier_type'] == _typeFilter;
      final category = (s['supplier_category'] ?? '').toString().trim();
      final matchesCategory =
          _categoryFilter == 'ALL' || category == _categoryFilter;
      final active = s['active'] == true;
      final matchesActive = _activeFilter == 'ALL' ||
          (_activeFilter == 'ACTIVE' && active) ||
          (_activeFilter == 'INACTIVE' && !active);
      if (!matchesType || !matchesCategory || !matchesActive) return false;
      if (query.isEmpty) return true;
      final name = (s['name'] ?? '').toString().toLowerCase();
      final email = (s['email'] ?? '').toString().toLowerCase();
      final supplierCategory =
          (s['supplier_category'] ?? '').toString().toLowerCase();
      final phone = (s['phone'] ?? '').toString().toLowerCase();
      final notes = (s['notes'] ?? '').toString().toLowerCase();
      return name.contains(query) ||
          email.contains(query) ||
          supplierCategory.contains(query) ||
          phone.contains(query) ||
          notes.contains(query);
    }).toList();

    int compareStrings(String a, String b) =>
        _sortAscending ? a.compareTo(b) : b.compareTo(a);
    int compareBools(bool a, bool b) => _sortAscending
        ? (a ? 1 : 0).compareTo(b ? 1 : 0)
        : (b ? 1 : 0).compareTo(a ? 1 : 0);

    filtered.sort((a, b) {
      switch (_sortColumnIndex) {
        case 0:
          return compareStrings(
            (a['name'] ?? '').toString().toLowerCase(),
            (b['name'] ?? '').toString().toLowerCase(),
          );
        case 1:
          return compareStrings(
            (a['email'] ?? '').toString().toLowerCase(),
            (b['email'] ?? '').toString().toLowerCase(),
          );
        case 2:
          return compareStrings(
            (a['supplier_type'] ?? '').toString(),
            (b['supplier_type'] ?? '').toString(),
          );
        case 3:
          return compareStrings(
            (a['supplier_category'] ?? '').toString().toLowerCase(),
            (b['supplier_category'] ?? '').toString().toLowerCase(),
          );
        case 4:
          return compareBools(a['active'] == true, b['active'] == true);
        default:
          return 0;
      }
    });

    return filtered;
  }

  List<String> get _availableCategories {
    final categories = _suppliers
        .map((s) => (s['supplier_category'] ?? '').toString().trim())
        .where((c) => c.isNotEmpty)
        .toSet()
        .toList()
      ..sort((a, b) => a.toLowerCase().compareTo(b.toLowerCase()));
    return categories;
  }

  Future<void> _showSupplierDialog({Map<String, dynamic>? supplier}) async {
    final nameController = TextEditingController(text: supplier?['name'] ?? '');
    final emailController =
        TextEditingController(text: supplier?['email'] ?? '');
    final categoryController =
        TextEditingController(text: supplier?['supplier_category'] ?? '');
    final phoneController =
        TextEditingController(text: supplier?['phone'] ?? '');
    final notesController =
        TextEditingController(text: supplier?['notes'] ?? '');
    String type = supplier?['supplier_type'] ?? 'MEDICAMENT';
    bool active = supplier?['active'] ?? true;

    final result = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(supplier == null ? 'Add Supplier' : 'Edit Supplier'),
        content: SizedBox(
          width: 420,
          child: SingleChildScrollView(
            child: Column(
              children: [
                TextField(
                    controller: nameController,
                    decoration: const InputDecoration(labelText: 'Name')),
                TextField(
                    controller: emailController,
                    decoration: const InputDecoration(labelText: 'Email')),
                const SizedBox(height: 12),
                DropdownButtonFormField<String>(
                  value: type,
                  items: const [
                    DropdownMenuItem(
                        value: 'MEDICAMENT', child: Text('MEDICAMENT')),
                    DropdownMenuItem(
                        value: 'MATERIEL', child: Text('MATERIEL')),
                  ],
                  onChanged: (value) => type = value ?? type,
                  decoration: const InputDecoration(labelText: 'Type'),
                ),
                TextField(
                  controller: categoryController,
                  decoration: const InputDecoration(
                    labelText: 'Category (sous-type libre)',
                  ),
                ),
                TextField(
                    controller: phoneController,
                    decoration: const InputDecoration(labelText: 'Phone')),
                TextField(
                    controller: notesController,
                    decoration: const InputDecoration(labelText: 'Notes')),
                CheckboxListTile(
                  value: active,
                  onChanged: (value) => active = value ?? true,
                  title: const Text('Active'),
                  controlAffinity: ListTileControlAffinity.leading,
                ),
              ],
            ),
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
    );

    if (result == true) {
      final payload = {
        'name': nameController.text.trim(),
        'email': emailController.text.trim(),
        'supplier_type': type,
        'supplier_category': categoryController.text.trim(),
        'phone': phoneController.text.trim(),
        'notes': notesController.text.trim(),
        'active': active,
      };
      try {
        if (supplier == null) {
          await SuppliersApi.create(payload);
        } else {
          await SuppliersApi.update(supplier['id'] as int, payload);
        }
        await _load();
      } catch (err) {
        if (mounted) {
          _showError(err);
        }
      }
    }
  }

  Future<void> _importSuppliers() async {
    final result = await FilePicker.platform.pickFiles(
      type: FileType.custom,
      allowedExtensions: ['csv'],
    );
    final path = result?.files.single.path;
    if (path == null) return;
    try {
      final response = await SuppliersApi.importCsv(path);
      final inserted = response['inserted'] ?? 0;
      final errors = (response['errors'] as List<dynamic>?) ?? [];
      if (!mounted) return;
      if (errors.isNotEmpty) {
        await showDialog<void>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('Import completed with errors'),
            content: SizedBox(
              width: 500,
              child: SingleChildScrollView(
                child: Column(
                  children: [
                    Text('Imported: $inserted'),
                    const SizedBox(height: 8),
                    ...errors.take(10).map(
                          (err) => Align(
                            alignment: Alignment.centerLeft,
                            child: Text('Row ${err['row']}: ${err['error']}'),
                          ),
                        ),
                    if (errors.length > 10)
                      Text('...and ${errors.length - 10} more errors'),
                  ],
                ),
              ),
            ),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(context),
                  child: const Text('Close')),
            ],
          ),
        );
      } else {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Imported $inserted suppliers')),
        );
      }
      await _load();
    } catch (err) {
      if (mounted) {
        _showError(err);
      }
    }
  }

  Future<void> _confirmDelete(Map<String, dynamic> supplier) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Deactivate Supplier'),
        content: const Text('Set supplier as inactive?'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          ElevatedButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Confirm')),
        ],
      ),
    );

    if (result == true) {
      try {
        await SuppliersApi.remove(supplier['id'] as int);
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
    final suppliers = _filteredSuppliers;
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Text('Suppliers',
                  style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
              const Spacer(),
              ElevatedButton(
                  onPressed: () => _showSupplierDialog(),
                  child: const Text('Add')),
              const SizedBox(width: 8),
              OutlinedButton(
                  onPressed: _importSuppliers, child: const Text('Import CSV')),
              const SizedBox(width: 8),
              OutlinedButton(onPressed: _load, child: const Text('Refresh')),
            ],
          ),
          const SizedBox(height: 16),
          Wrap(
            spacing: 12,
            runSpacing: 12,
            children: [
              SizedBox(
                width: 260,
                child: TextField(
                  controller: _searchController,
                  decoration: InputDecoration(
                    labelText: 'Search suppliers',
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
              DropdownButton<String>(
                value: _typeFilter,
                items: const [
                  DropdownMenuItem(value: 'ALL', child: Text('All types')),
                  DropdownMenuItem(
                      value: 'MEDICAMENT', child: Text('MEDICAMENT')),
                  DropdownMenuItem(value: 'MATERIEL', child: Text('MATERIEL')),
                ],
                onChanged: (value) =>
                    setState(() => _typeFilter = value ?? 'ALL'),
              ),
              DropdownButton<String>(
                value: _categoryFilter,
                items: [
                  const DropdownMenuItem(
                      value: 'ALL', child: Text('All categories')),
                  ..._availableCategories.map(
                    (c) => DropdownMenuItem(value: c, child: Text(c)),
                  ),
                ],
                onChanged: (value) =>
                    setState(() => _categoryFilter = value ?? 'ALL'),
              ),
              DropdownButton<String>(
                value: _activeFilter,
                items: const [
                  DropdownMenuItem(value: 'ALL', child: Text('All status')),
                  DropdownMenuItem(value: 'ACTIVE', child: Text('Active')),
                  DropdownMenuItem(value: 'INACTIVE', child: Text('Inactive')),
                ],
                onChanged: (value) =>
                    setState(() => _activeFilter = value ?? 'ALL'),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Expanded(
            child: suppliers.isEmpty
                ? EmptyState(
                    icon: Icons.local_shipping_outlined,
                    title: _suppliers.isEmpty
                        ? 'No suppliers yet'
                        : 'No suppliers match filters',
                    message: _suppliers.isEmpty
                        ? 'Create your first supplier to start building campaigns.'
                        : 'Try adjusting your filters or search.',
                    action: _suppliers.isEmpty
                        ? ElevatedButton(
                            onPressed: () => _showSupplierDialog(),
                            child: const Text('Add supplier'),
                          )
                        : null,
                  )
                : SingleChildScrollView(
                    child: DataTable(
                      sortAscending: _sortAscending,
                      sortColumnIndex: _sortColumnIndex,
                      columns: [
                        DataColumn(
                          label: const Text('Name'),
                          onSort: (index, asc) => setState(() {
                            _sortColumnIndex = index;
                            _sortAscending = asc;
                          }),
                        ),
                        DataColumn(
                          label: const Text('Email'),
                          onSort: (index, asc) => setState(() {
                            _sortColumnIndex = index;
                            _sortAscending = asc;
                          }),
                        ),
                        DataColumn(
                          label: const Text('Type'),
                          onSort: (index, asc) => setState(() {
                            _sortColumnIndex = index;
                            _sortAscending = asc;
                          }),
                        ),
                        DataColumn(
                          label: const Text('Category'),
                          onSort: (index, asc) => setState(() {
                            _sortColumnIndex = index;
                            _sortAscending = asc;
                          }),
                        ),
                        DataColumn(
                          label: const Text('Active'),
                          onSort: (index, asc) => setState(() {
                            _sortColumnIndex = index;
                            _sortAscending = asc;
                          }),
                        ),
                        const DataColumn(label: Text('Actions')),
                      ],
                      rows: suppliers
                          .map(
                            (s) => DataRow(cells: [
                              DataCell(Text(s['name'] as String)),
                              DataCell(Text(s['email'] as String)),
                              DataCell(Text(s['supplier_type'] as String)),
                              DataCell(Text((s['supplier_category'] ?? '')
                                      .toString()
                                      .trim()
                                      .isEmpty
                                  ? '-'
                                  : s['supplier_category'].toString())),
                              DataCell(
                                  Text((s['active'] == true) ? 'Yes' : 'No')),
                              DataCell(Row(
                                children: [
                                  IconButton(
                                    icon: const Icon(Icons.edit),
                                    onPressed: () => _showSupplierDialog(
                                        supplier: s as Map<String, dynamic>),
                                  ),
                                  IconButton(
                                    icon: const Icon(Icons.delete),
                                    onPressed: () => _confirmDelete(
                                        s as Map<String, dynamic>),
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
