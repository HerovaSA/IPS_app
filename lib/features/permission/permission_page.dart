import 'package:flutter/foundation.dart';
import 'package:parliament_ips/controllers/permission_controller.dart';
import 'package:parliament_ips/core/localization/locale_controller.dart';
import 'package:parliament_ips/core/theme/app_palette.dart';
import 'package:parliament_ips/core/theme/theme_controller.dart';
import 'package:parliament_ips/features/navigation/views/main_navigation_screen.dart';
import 'package:parliament_ips/features/onboarding/onboarding_page.dart';
import 'package:parliament_ips/features/permission/widgets/permission_checklist_card.dart';
import 'package:parliament_ips/features/permission/widgets/permission_status_card.dart';
import 'package:flutter/material.dart';
import 'package:flutter_reactive_ble/flutter_reactive_ble.dart';
import 'package:get/get.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// أنواع حالات صفحة الأذونات — كل حالة بترسم واجهة مختلفة بنفس الصفحة.
enum _PermissionUiState {
  locationOnly, // الموقع مرفوض بس، البلوتوث تمام
  bluetoothOffOnly, // البلوتوث مقفول بس (adapter)، الموقع تمام
  both, // الاتنين ناقصين (أو صلاحية البلوتوث نفسها مرفوضة)
  allSet, // كل حاجة متفعّلة
}

/// صفحة الأذونات — صفحة واحدة بترسم نفسها بشكل مختلف حسب حالة الأذونات
/// الحالية (4 حالات: الموقع بس / البلوتوث بس / الاتنين / كل حاجة تمام).
class PermissionPage extends StatelessWidget {
  final permissionController = Get.find<PermissionController>();

  PermissionPage({super.key});

  _PermissionUiState _resolveState() {
    final locationOk = permissionController.locationPermissionGranted.value;
    final bluetoothOk = permissionController.bluetoothStatus.value;

    if (locationOk && bluetoothOk) return _PermissionUiState.allSet;

    if (!locationOk && bluetoothOk) return _PermissionUiState.locationOnly;

    if (locationOk &&
        !bluetoothOk &&
        permissionController.bleStatusRaw.value == BleStatus.poweredOff) {
      return _PermissionUiState.bluetoothOffOnly;
    }

    return _PermissionUiState.both;
  }

  Future<void> _handleStartNavigation() async {
    await permissionController.checkPermissionStatus();
    final bool canProceed = kDebugMode ||
        (permissionController.locationPermissionGranted.value == true &&
            permissionController.bluetoothStatus.value == true);

    if (canProceed) {
      final prefs = await SharedPreferences.getInstance();
      final onboardingDone = prefs.getBool('initial') == true;
      if (onboardingDone) {
        Get.offAll(const MainNavigationScreen());
      } else {
        Get.offAll(OnboardingPage());
      }
    } else {
      Get.rawSnackbar(
        titleText: const Text(
          'Error',
          style: TextStyle(
            color: Colors.white,
            fontWeight: FontWeight.w800,
            fontSize: 16,
          ),
        ),
        messageText: Text(
          'يرجى تفعيل الأذونات أعلاه.'.tr,
          style: const TextStyle(color: Colors.white, fontSize: 16),
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final themeController = Get.find<ThemeController>();
    final localeController = Get.find<LocaleController>();

    return Obx(() {
      final palette = AppPalette.of(themeController.isDarkMode.value);
      final state = _resolveState();

      return Directionality(
        textDirection:
            localeController.isArabic ? TextDirection.rtl : TextDirection.ltr,
        child: Scaffold(
          backgroundColor: palette.background,
          body: SafeArea(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 32),
              child: Center(
                child: SingleChildScrollView(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      _buildForState(context, state, palette),
                      if (kDebugMode) ...[
                        const SizedBox(height: 20),
                        ElevatedButton.icon(
                          style: ElevatedButton.styleFrom(
                            backgroundColor: palette.goldDark,
                            foregroundColor: Colors.white,
                            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
                            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
                          ),
                          icon: const Icon(Icons.map_rounded),
                          label: const Text(
                            'فتح الخريطة والشاشات (وضع التجربة)',
                            style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
                          ),
                          onPressed: () => Get.offAll(const MainNavigationScreen()),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      );
    });
  }

  Widget _buildForState(
    BuildContext context,
    _PermissionUiState state,
    AppPalette palette,
  ) {
    switch (state) {
      case _PermissionUiState.locationOnly:
        return PermissionStatusCard(
          palette: palette,
          illustrationIcon: Icons.location_on_rounded,
          badgeIcon: Icons.close_rounded,
          title: 'السماح مطلوب',
          description:
              'يبدو أنك لم تسمح للتطبيق بالوصول المطلوب. لتتمكن من استخدام التوجيه داخل المبنى، يرجى تفعيل الصلاحية من إعدادات التطبيق.',
          primaryButtonLabel: 'فتح الإعدادات',
          onPrimaryPressed: () => openAppSettings(),
          secondaryButtonLabel: 'حاول مرة أخرى',
          onSecondaryPressed: () => permissionController.checkPermissionStatus(),
        );

      case _PermissionUiState.bluetoothOffOnly:
        return PermissionStatusCard(
          palette: palette,
          illustrationIcon: Icons.bluetooth_rounded,
          toggleLabel: 'OFF',
          badgeIcon: Icons.bluetooth_disabled_rounded,
          title: 'البلوتوث متوقف',
          description: 'قم بتشغيل البلوتوث للبحث عن إشارات التوجيه داخل المبنى.',
          primaryButtonLabel: 'تشغيل البلوتوث',
          onPrimaryPressed: () => permissionController.requestEnableBluetooth(),
          secondaryButtonLabel: 'حاول مرة أخرى',
          onSecondaryPressed: () => permissionController.checkPermissionStatus(),
        );

      case _PermissionUiState.both:
        final locationOk = permissionController.locationPermissionGranted.value;
        final bluetoothOk = permissionController.bluetoothStatus.value;
        final bluetoothAdapterOff =
            permissionController.bleStatusRaw.value == BleStatus.poweredOff;

        return PermissionChecklistCard(
          palette: palette,
          title: 'هناك أذونات مطلوبة',
          description:
              'للاستمرار في استخدام التوجيه داخل المبني، يرجى تفعيل الأذونات التالية.',
          items: [
            PermissionChecklistItem(
              icon: Icons.bluetooth_rounded,
              title: 'البلوتوث',
              statusLabel: bluetoothOk ? 'مفعّل' : 'متوقف',
              isGranted: bluetoothOk,
              actionLabel: bluetoothAdapterOff ? 'تشغيل' : 'الإعدادات',
              onAction: () {
                if (bluetoothAdapterOff) {
                  permissionController.requestEnableBluetooth();
                } else {
                  openAppSettings();
                }
              },
            ),
            PermissionChecklistItem(
              icon: Icons.location_on_rounded,
              title: 'الموقع',
              statusLabel: locationOk ? 'مفعّل' : 'غير مفعّلة',
              isGranted: locationOk,
              actionLabel: 'الإعدادات',
              onAction: () => openAppSettings(),
            ),
          ],
          onRetry: () => permissionController.checkPermissionStatus(),
        );

      case _PermissionUiState.allSet:
        return PermissionStatusCard(
          palette: palette,
          illustrationIcon: Icons.check_rounded,
          standalone: true,
          title: 'كل شيء جاهز',
          description:
              'تم تفعيل جميع الأذونات المطلوبة. يمكنك الآن استخدام التوجيه داخل المبنى.',
          primaryButtonLabel: 'ابدأ التوجيه',
          onPrimaryPressed: _handleStartNavigation,
        );
    }
  }
}
