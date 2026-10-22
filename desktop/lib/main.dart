import 'package:flutter/material.dart';
import 'package:flutter/gestures.dart';
import 'package:provider/provider.dart';

import 'auth_state.dart';
import 'screens/campaigns.dart';
import 'screens/dashboard.dart';
import 'screens/groups.dart';
import 'screens/login.dart';
import 'screens/best_offers.dart';
import 'screens/products.dart';
import 'screens/results.dart';
import 'screens/gpt_analysis.dart';
import 'screens/strict_campaigns.dart';
import 'screens/verification.dart';
import 'screens/imported_files.dart';
import 'screens/settings.dart';
import 'screens/suppliers.dart';
import 'screens/admin.dart';
import 'screens/templates.dart';
import 'widgets/sidebar.dart';
import 'widgets/app_background.dart';
import 'theme.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final authState = AuthState();
  await authState.load();
  runApp(MyApp(authState: authState));
}

class _DesktopScrollBehavior extends MaterialScrollBehavior {
  const _DesktopScrollBehavior();

  static const Set<TargetPlatform> _desktopPlatforms = {
    TargetPlatform.windows,
    TargetPlatform.macOS,
    TargetPlatform.linux,
  };

  @override
  Set<PointerDeviceKind> get dragDevices => const {
        PointerDeviceKind.touch,
        PointerDeviceKind.mouse,
        PointerDeviceKind.stylus,
        PointerDeviceKind.invertedStylus,
        PointerDeviceKind.unknown,
      };

  @override
  Widget buildOverscrollIndicator(
    BuildContext context,
    Widget child,
    ScrollableDetails details,
  ) {
    return child;
  }

  @override
  Widget buildScrollbar(
    BuildContext context,
    Widget child,
    ScrollableDetails details,
  ) {
    if (!_desktopPlatforms.contains(getPlatform(context))) {
      return super.buildScrollbar(context, child, details);
    }
    return Scrollbar(
      controller: details.controller,
      thumbVisibility: true,
      trackVisibility: true,
      interactive: true,
      thickness: 12,
      radius: const Radius.circular(999),
      child: child,
    );
  }
}

class MyApp extends StatelessWidget {
  final AuthState authState;

  const MyApp({super.key, required this.authState});

  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider<AuthState>.value(
      value: authState,
      child: MaterialApp(
        title: 'FactuRoll',
        theme: AppTheme.light(),
        scrollBehavior: const _DesktopScrollBehavior(),
        home: Consumer<AuthState>(
          builder: (context, auth, _) {
            if (auth.isLoading) {
              return const Scaffold(
                body: Center(child: CircularProgressIndicator()),
              );
            }
            if (auth.isLoggedIn) {
              return const HomeShell();
            }
            return const LoginScreen();
          },
        ),
      ),
    );
  }
}

class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;
  final Map<int, Widget> _screenCache = <int, Widget>{};
  static const Set<int> _refreshOnSelectIndexes = {4, 7, 8, 11};
  static const Set<int> _lockedIndexes = {6, 9, 10};

  Widget _createScreen(int index, bool isAdmin) {
    switch (index) {
      case 0:
        return const DashboardScreen();
      case 1:
        return const SuppliersScreen();
      case 2:
        return const GroupsScreen();
      case 3:
        return const ProductsScreen();
      case 4:
        return const CampaignsScreen();
      case 5:
        return const TemplatesScreen();
      case 6:
        return const ResultsScreen();
      case 7:
        return const GptAnalysisScreen();
      case 8:
        return const StrictCampaignsScreen();
      case 9:
        return const BestOffersScreen();
      case 10:
        return const VerificationScreen();
      case 11:
        return const ImportedFilesScreen();
      case 12:
        return const SettingsScreen();
      case 13:
        if (isAdmin) {
          return const AdminScreen();
        }
        return const DashboardScreen();
      default:
        return const DashboardScreen();
    }
  }

  Widget _screenFor(int index, bool isAdmin) {
    return _screenCache.putIfAbsent(index, () => _createScreen(index, isAdmin));
  }

  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthState>();
    final isAdmin = auth.isAdmin;
    final screenCount = isAdmin ? 14 : 13;

    if (_index >= screenCount) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) {
          setState(() => _index = 0);
        }
      });
    }

    final selectedIndex = _index < screenCount ? _index : 0;
    return Scaffold(
      body: AppBackground(
        child: SafeArea(
          child: Row(
            children: [
              Sidebar(
                selectedIndex: selectedIndex,
                lockedIndexes: _lockedIndexes,
                onSelect: (index) {
                  if (_lockedIndexes.contains(index)) {
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(
                        content: Text('Ecran verrouille pour le moment'),
                      ),
                    );
                    return;
                  }
                  setState(() {
                    if (_refreshOnSelectIndexes.contains(index)) {
                      _screenCache.remove(index);
                    }
                    _index = index;
                  });
                },
                onLogout: () => context.read<AuthState>().logout(),
                isAdmin: isAdmin,
              ),
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      color: Theme.of(context)
                          .colorScheme
                          .surface
                          .withOpacity(0.92),
                      borderRadius: BorderRadius.circular(22),
                      border: Border.all(
                        color: Theme.of(context)
                            .colorScheme
                            .outline
                            .withOpacity(0.6),
                      ),
                      boxShadow: [
                        BoxShadow(
                          color: Colors.black.withOpacity(0.08),
                          blurRadius: 16,
                          offset: const Offset(0, 6),
                        ),
                      ],
                    ),
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(22),
                      child: _screenFor(selectedIndex, isAdmin),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
