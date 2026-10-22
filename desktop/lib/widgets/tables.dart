import 'package:flutter/material.dart';

class SimpleTable extends StatelessWidget {
  final List<String> headers;
  final List<List<Widget>> rows;

  const SimpleTable({
    super.key,
    required this.headers,
    required this.rows,
  });

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: DataTable(
        columns: headers.map((h) => DataColumn(label: Text(h))).toList(),
        rows: rows
            .map(
              (cells) => DataRow(
                cells: cells.map((cell) => DataCell(cell)).toList(),
              ),
            )
            .toList(),
      ),
    );
  }
}
