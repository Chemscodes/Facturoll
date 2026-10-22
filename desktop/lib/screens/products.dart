import 'package:flutter/material.dart';
import 'package:file_picker/file_picker.dart';
import 'dart:convert';

import '../api/client.dart';
import '../api/products_api.dart';
import '../widgets/empty_state.dart';

class ProductsScreen extends StatefulWidget {
  const ProductsScreen({super.key});

  @override
  State<ProductsScreen> createState() => _ProductsScreenState();
}

class _ProductsScreenState extends State<ProductsScreen> {
  bool _loading = true;
  String? _error;
  List<dynamic> _products = [];
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
      _products = await ProductsApi.list();
    } catch (err) {
      _error = ApiClient.formatError(err);
    }
    if (mounted) {
      setState(() => _loading = false);
    }
  }

  List<dynamic> get _filteredProducts {
    final query = _searchQuery.trim().toLowerCase();
    final filtered = _products.where((p) {
      if (query.isEmpty) return true;
      final name = (p['name'] ?? '').toString().toLowerCase();
      final sku = (p['sku'] ?? '').toString().toLowerCase();
      final unit = (p['unit'] ?? '').toString().toLowerCase();
      return name.contains(query) ||
          sku.contains(query) ||
          unit.contains(query);
    }).toList();

    int compareStrings(String a, String b) =>
        _sortAscending ? a.compareTo(b) : b.compareTo(a);

    filtered.sort((a, b) {
      switch (_sortColumnIndex) {
        case 0:
          return compareStrings(
            (a['name'] ?? '').toString().toLowerCase(),
            (b['name'] ?? '').toString().toLowerCase(),
          );
        case 1:
          return compareStrings(
            (a['sku'] ?? '').toString().toLowerCase(),
            (b['sku'] ?? '').toString().toLowerCase(),
          );
        case 2:
          return compareStrings(
            (a['unit'] ?? '').toString().toLowerCase(),
            (b['unit'] ?? '').toString().toLowerCase(),
          );
        default:
          return 0;
      }
    });

    return filtered;
  }

  Future<void> _showProductDialog({Map<String, dynamic>? product}) async {
    final nameController = TextEditingController(text: product?['name'] ?? '');
    final skuController = TextEditingController(text: product?['sku'] ?? '');
    final unitController = TextEditingController(text: product?['unit'] ?? '');
    final descriptionController =
        TextEditingController(text: product?['description'] ?? '');
    final characteristicsRaw = product?['characteristics'];
    final characteristicsController = TextEditingController(
      text: (characteristicsRaw is Map && characteristicsRaw.isNotEmpty)
          ? const JsonEncoder.withIndent('  ').convert(characteristicsRaw)
          : '',
    );

    final result = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(product == null ? 'Add Product' : 'Edit Product'),
        content: SizedBox(
          width: 420,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                  controller: nameController,
                  decoration: const InputDecoration(labelText: 'Name')),
              TextField(
                  controller: skuController,
                  decoration: const InputDecoration(labelText: 'SKU')),
              TextField(
                  controller: unitController,
                  decoration: const InputDecoration(labelText: 'Unit')),
              TextField(
                controller: descriptionController,
                decoration:
                    const InputDecoration(labelText: 'Description (optional)'),
                maxLines: 3,
              ),
              TextField(
                controller: characteristicsController,
                decoration: const InputDecoration(
                  labelText: 'Characteristics JSON (optional)',
                  hintText: '{"tension":["200-240V"],"puissance":["100W"]}',
                ),
                maxLines: 5,
              ),
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
    );

    if (result == true) {
      Map<String, dynamic>? characteristics;
      final rawChars = characteristicsController.text.trim();
      if (rawChars.isNotEmpty) {
        try {
          final parsed = jsonDecode(rawChars);
          if (parsed is! Map) {
            throw const FormatException('JSON must be an object');
          }
          characteristics = Map<String, dynamic>.from(parsed);
        } catch (_) {
          if (mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
              const SnackBar(
                content: Text('Characteristics JSON invalide'),
              ),
            );
          }
          return;
        }
      }

      final payload = {
        'name': nameController.text.trim(),
        'sku': skuController.text.trim(),
        'unit': unitController.text.trim(),
        'description': descriptionController.text.trim().isEmpty
            ? null
            : descriptionController.text.trim(),
        'characteristics': characteristics,
      };
      try {
        if (product == null) {
          await ProductsApi.create(payload);
        } else {
          await ProductsApi.update(product['id'] as int, payload);
        }
        await _load();
      } catch (err) {
        if (mounted) {
          _showError(err);
        }
      }
    }
  }

  Future<void> _deleteProduct(Map<String, dynamic> product) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete Product'),
        content: const Text('Delete this product?'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          ElevatedButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Delete')),
        ],
      ),
    );
    if (result == true) {
      try {
        await ProductsApi.remove(product['id'] as int);
        await _load();
      } catch (err) {
        if (mounted) {
          _showError(err);
        }
      }
    }
  }

  Future<void> _importProducts() async {
    final result = await FilePicker.platform.pickFiles(
      type: FileType.custom,
      allowedExtensions: ['csv'],
    );
    final path = result?.files.single.path;
    if (path == null) return;
    try {
      final response = await ProductsApi.importCsv(path);
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
          SnackBar(content: Text('Imported $inserted products')),
        );
      }
      await _load();
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
    final products = _filteredProducts;
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Text('Products',
                  style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
              const Spacer(),
              ElevatedButton(
                  onPressed: () => _showProductDialog(),
                  child: const Text('Add')),
              const SizedBox(width: 8),
              OutlinedButton(
                  onPressed: _importProducts, child: const Text('Import CSV')),
              const SizedBox(width: 8),
              OutlinedButton(onPressed: _load, child: const Text('Refresh')),
            ],
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: 260,
            child: TextField(
              controller: _searchController,
              decoration: InputDecoration(
                labelText: 'Search products',
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
            child: products.isEmpty
                ? EmptyState(
                    icon: Icons.inventory_2_outlined,
                    title: _products.isEmpty
                        ? 'No products yet'
                        : 'No products match search',
                    message: _products.isEmpty
                        ? 'Add products so you can include them in campaigns.'
                        : 'Try a different keyword.',
                    action: _products.isEmpty
                        ? ElevatedButton(
                            onPressed: () => _showProductDialog(),
                            child: const Text('Add product'),
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
                          label: const Text('SKU'),
                          onSort: (index, asc) => setState(() {
                            _sortColumnIndex = index;
                            _sortAscending = asc;
                          }),
                        ),
                        DataColumn(
                          label: const Text('Unit'),
                          onSort: (index, asc) => setState(() {
                            _sortColumnIndex = index;
                            _sortAscending = asc;
                          }),
                        ),
                        const DataColumn(label: Text('Actions')),
                      ],
                      rows: products
                          .map(
                            (p) => DataRow(cells: [
                              DataCell(Text(p['name'] as String)),
                              DataCell(Text(p['sku'] ?? '')),
                              DataCell(Text(p['unit'] ?? '')),
                              DataCell(Row(
                                children: [
                                  IconButton(
                                    icon: const Icon(Icons.edit),
                                    onPressed: () => _showProductDialog(
                                        product: p as Map<String, dynamic>),
                                  ),
                                  IconButton(
                                    icon: const Icon(Icons.delete),
                                    onPressed: () => _deleteProduct(
                                        p as Map<String, dynamic>),
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
