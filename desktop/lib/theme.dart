import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

class AppTheme {
  static ThemeData light() {
    const primary = Color(0xFF1F4B5C);
    const secondary = Color(0xFFF2A65A);
    const surface = Color(0xFFFDF9F3);
    const background = Color(0xFFF6F2EA);
    const outline = Color(0xFFD7CFC4);

    final colorScheme = ColorScheme.fromSeed(
      seedColor: primary,
      brightness: Brightness.light,
    ).copyWith(
      primary: primary,
      secondary: secondary,
      surface: surface,
      background: background,
      outline: outline,
      surfaceVariant: const Color(0xFFECE5DA),
      onSurface: const Color(0xFF1E2428),
      onBackground: const Color(0xFF1E2428),
    );

    final baseTextTheme = ThemeData(brightness: Brightness.light).textTheme;
    final textTheme = GoogleFonts.spaceGroteskTextTheme(baseTextTheme).copyWith(
      titleLarge: GoogleFonts.spaceGrotesk(fontWeight: FontWeight.w700),
      titleMedium: GoogleFonts.spaceGrotesk(fontWeight: FontWeight.w600),
      headlineSmall: GoogleFonts.spaceGrotesk(fontWeight: FontWeight.w700),
    );

    return ThemeData(
      useMaterial3: true,
      colorScheme: colorScheme,
      textTheme: textTheme,
      scaffoldBackgroundColor: Colors.transparent,
      visualDensity: VisualDensity.standard,
      cardTheme: CardThemeData(
        color: colorScheme.surface,
        elevation: 1,
        shadowColor: Colors.black.withOpacity(0.08),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(16),
          side: BorderSide(color: colorScheme.outline.withOpacity(0.5)),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: colorScheme.surface,
        contentPadding:
            const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: BorderSide(color: colorScheme.outline),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: BorderSide(color: colorScheme.outline.withOpacity(0.7)),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: BorderSide(color: colorScheme.primary, width: 1.5),
        ),
      ),
      elevatedButtonTheme: ElevatedButtonThemeData(
        style: ElevatedButton.styleFrom(
          padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 14),
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          side: BorderSide(color: colorScheme.outline),
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          shape:
              RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
        ),
      ),
      navigationRailTheme: NavigationRailThemeData(
        backgroundColor: Colors.transparent,
        indicatorColor: colorScheme.secondary.withOpacity(0.22),
        indicatorShape:
            RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        selectedIconTheme: IconThemeData(color: colorScheme.onPrimary),
        unselectedIconTheme:
            IconThemeData(color: colorScheme.onPrimary.withOpacity(0.75)),
        selectedLabelTextStyle:
            textTheme.labelMedium?.copyWith(color: colorScheme.onPrimary),
        unselectedLabelTextStyle: textTheme.labelMedium
            ?.copyWith(color: colorScheme.onPrimary.withOpacity(0.7)),
      ),
      dataTableTheme: DataTableThemeData(
        headingRowColor: MaterialStatePropertyAll(colorScheme.surfaceVariant),
        headingTextStyle:
            textTheme.labelLarge?.copyWith(fontWeight: FontWeight.w700),
        dataTextStyle: textTheme.bodyMedium,
        dividerThickness: 0.6,
      ),
      snackBarTheme: SnackBarThemeData(
        backgroundColor: colorScheme.onSurface,
        contentTextStyle:
            textTheme.bodyMedium?.copyWith(color: colorScheme.surface),
        behavior: SnackBarBehavior.floating,
      ),
      dialogTheme: DialogThemeData(
        backgroundColor: colorScheme.surface,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(18)),
      ),
      scrollbarTheme: ScrollbarThemeData(
        thumbVisibility: const MaterialStatePropertyAll(true),
        trackVisibility: const MaterialStatePropertyAll(true),
        interactive: true,
        thickness: const MaterialStatePropertyAll(12),
        radius: const Radius.circular(999),
        thumbColor: MaterialStateProperty.resolveWith((states) {
          if (states.contains(MaterialState.dragged)) {
            return colorScheme.primary.withOpacity(0.95);
          }
          if (states.contains(MaterialState.hovered)) {
            return colorScheme.primary.withOpacity(0.80);
          }
          return colorScheme.primary.withOpacity(0.58);
        }),
        trackColor: MaterialStatePropertyAll(
          colorScheme.surfaceVariant.withOpacity(0.72),
        ),
        trackBorderColor: MaterialStatePropertyAll(
          colorScheme.outline.withOpacity(0.32),
        ),
      ),
    );
  }
}
