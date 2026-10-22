import 'dart:io';

Future<bool> _runAndCheck(String executable, List<String> args) async {
  try {
    final result = await Process.run(executable, args);
    return result.exitCode == 0;
  } catch (_) {
    return false;
  }
}

bool _isWebUrl(String value) {
  final uri = Uri.tryParse(value.trim());
  if (uri == null || !uri.hasScheme) {
    return false;
  }
  final scheme = uri.scheme.toLowerCase();
  return scheme == 'http' || scheme == 'https';
}

bool isLocalWindowsPath(String value) {
  final normalized = value.trim();
  if (normalized.isEmpty) {
    return false;
  }
  return RegExp(r'^[A-Za-z]:\\').hasMatch(normalized) ||
      normalized.startsWith('\\\\');
}

String safeTempPdfName(String preferred, String fallbackBase) {
  final raw = preferred.trim().isEmpty ? fallbackBase : preferred.trim();
  var safe = raw.replaceAll(RegExp(r'[<>:"/\\|?*]'), '_');
  if (!safe.toLowerCase().endsWith('.pdf')) {
    safe = '$safe.pdf';
  }
  return safe;
}

Future<bool> openWithShell(String value) async {
  final target = value.trim();
  if (target.isEmpty) return false;
  try {
    if (Platform.isWindows) {
      final escapedDoubleQuote = target.replaceAll('"', '""');
      final isWebUrl = _isWebUrl(target);

      if (isWebUrl) {
        // Explicit default-browser opening on Windows for OAuth links.
        if (await _runAndCheck(
            'rundll32', ['url.dll,FileProtocolHandler', target])) {
          return true;
        }
        if (await _runAndCheck(
            'cmd', ['/c', 'start "" "$escapedDoubleQuote"'])) {
          return true;
        }
      } else {
        if (await _runAndCheck('explorer.exe', [target])) {
          return true;
        }
      }

      if (await _runAndCheck(
        'powershell',
        [
          '-NoProfile',
          '-ExecutionPolicy',
          'Bypass',
          '-Command',
          'Start-Process -FilePath "$escapedDoubleQuote"',
        ],
      )) {
        return true;
      }

      return false;
    }
    if (Platform.isMacOS) {
      return _runAndCheck('open', [target]);
    }
    return _runAndCheck('xdg-open', [target]);
  } catch (_) {
    return false;
  }
}

Future<String> writeTempPdf({
  required List<int> bytes,
  required String preferredName,
  required String fallbackBase,
  String prefix = 'trial',
}) async {
  final safeName = safeTempPdfName(preferredName, fallbackBase);
  final filePath =
      '${Directory.systemTemp.path}${Platform.pathSeparator}${prefix}_${DateTime.now().millisecondsSinceEpoch}_$safeName';
  final out = File(filePath);
  await out.writeAsBytes(bytes, flush: true);
  return out.path;
}

Future<bool> openTempPdf({
  required List<int> bytes,
  required String preferredName,
  required String fallbackBase,
  String prefix = 'trial',
}) async {
  final path = await writeTempPdf(
    bytes: bytes,
    preferredName: preferredName,
    fallbackBase: fallbackBase,
    prefix: prefix,
  );
  return openWithShell(path);
}
