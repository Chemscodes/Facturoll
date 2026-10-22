import 'package:flutter/material.dart';
import 'package:flutter_html/flutter_html.dart';

class HtmlPreview extends StatelessWidget {
  final String html;

  const HtmlPreview({super.key, required this.html});

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      child: Html(data: html),
    );
  }
}
