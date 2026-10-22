// This is a basic Flutter widget test.
//
// To perform an interaction with a widget in your test, use the WidgetTester
// utility in the flutter_test package. For example, you can send tap and scroll
// gestures. You can also use WidgetTester to find child widgets in the widget
// tree, read text, and verify that the values of widget properties are correct.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:trial_desktop/auth_state.dart';
import 'package:trial_desktop/main.dart';

void main() {
  testWidgets('App shows loading state', (WidgetTester tester) async {
    final authState = AuthState();
    await tester.pumpWidget(MyApp(authState: authState));

    expect(find.byType(CircularProgressIndicator), findsOneWidget);
  });
}
