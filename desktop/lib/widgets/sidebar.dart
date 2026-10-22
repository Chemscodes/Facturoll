import 'package:flutter/material.dart';

class Sidebar extends StatelessWidget {
  final int selectedIndex;
  final ValueChanged<int> onSelect;
  final VoidCallback onLogout;
  final bool isAdmin;
  final Set<int> lockedIndexes;

  const Sidebar({
    super.key,
    required this.selectedIndex,
    required this.onSelect,
    required this.onLogout,
    required this.isAdmin,
    required this.lockedIndexes,
  });

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final items = <_NavItem>[
      const _NavItem(icon: Icons.dashboard, label: 'Dashboard'),
      const _NavItem(icon: Icons.local_shipping, label: 'Suppliers'),
      const _NavItem(icon: Icons.group, label: 'Groups'),
      const _NavItem(icon: Icons.inventory_2, label: 'Products'),
      const _NavItem(icon: Icons.campaign, label: 'Campaigns'),
      const _NavItem(icon: Icons.description, label: 'Templates'),
      const _NavItem(icon: Icons.bar_chart, label: 'Results'),
      const _NavItem(icon: Icons.smart_toy_outlined, label: 'Analyses'),
      const _NavItem(icon: Icons.rule_folder_outlined, label: 'Strict'),
      const _NavItem(
        icon: Icons.workspace_premium_outlined,
        label: 'Top Offres',
      ),
      const _NavItem(icon: Icons.fact_check, label: 'A verifier'),
      const _NavItem(icon: Icons.move_to_inbox_outlined, label: 'Fichiers importes'),
      const _NavItem(icon: Icons.settings, label: 'Settings'),
    ];
    if (isAdmin) {
      items.add(
        const _NavItem(icon: Icons.admin_panel_settings, label: 'Admin'),
      );
    }
    return Container(
      width: 220,
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [
            scheme.primary,
            scheme.primary.withOpacity(0.92),
            scheme.primary.withOpacity(0.85),
          ],
        ),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withOpacity(0.12),
            blurRadius: 18,
            offset: const Offset(2, 0),
          ),
        ],
      ),
      child: SafeArea(
        child: Column(
          children: [
            const SizedBox(height: 16),
            Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Container(
                  width: 36,
                  height: 36,
                  decoration: BoxDecoration(
                    color: scheme.secondary,
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: Icon(Icons.bolt, color: scheme.onSecondary),
                ),
                const SizedBox(width: 10),
                Text(
                  'TRIAL',
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                        color: scheme.onPrimary,
                        letterSpacing: 2.2,
                      ),
                ),
              ],
            ),
            const SizedBox(height: 18),
            Expanded(
              child: ListView.builder(
                padding: EdgeInsets.zero,
                itemCount: items.length,
                itemBuilder: (context, index) {
                  final item = items[index];
                  final selected = index == selectedIndex;
                  final locked = lockedIndexes.contains(index);
                  return ListTile(
                    dense: true,
                    leading: Icon(
                      locked ? Icons.lock_outline : item.icon,
                      color: selected
                          ? scheme.onPrimary
                          : scheme.onPrimary.withOpacity(locked ? 0.55 : 0.8),
                    ),
                    title: Text(
                      item.label,
                      style: TextStyle(
                        color: scheme.onPrimary.withOpacity(locked ? 0.72 : 1),
                        fontWeight:
                            selected ? FontWeight.bold : FontWeight.w500,
                        fontSize: 13,
                      ),
                    ),
                    selected: selected,
                    selectedTileColor: scheme.onPrimary.withOpacity(
                      locked ? 0.08 : 0.12,
                    ),
                    onTap: () => onSelect(index),
                  );
                },
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
              child: OutlinedButton.icon(
                onPressed: onLogout,
                icon: const Icon(Icons.logout),
                label: const Text('Logout'),
                style: OutlinedButton.styleFrom(
                  foregroundColor: scheme.onPrimary,
                  side: BorderSide(color: scheme.onPrimary.withOpacity(0.5)),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _NavItem {
  final IconData icon;
  final String label;
  const _NavItem({required this.icon, required this.label});
}
