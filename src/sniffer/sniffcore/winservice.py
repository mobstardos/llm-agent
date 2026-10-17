# -*- coding: utf-8 -*-
"""
Служба Windows без сторонних зависимостей (только ctypes из stdlib).

Позволяет установить сниффер как службу Windows с автозапуском:
    sniffer.py --service install    установить службу (автостарт при загрузке)
    sniffer.py --service start      запустить
    sniffer.py --service stop       остановить
    sniffer.py --service restart    перезапустить
    sniffer.py --service status     состояние
    sniffer.py --service remove     удалить службу
    sniffer.py --service run        служебный режим (вызывается диспетчером SCM)

Установка/удаление/запуск/остановка требуют прав администратора.
Служба стартует с ключом "--service run": процесс регистрируется в SCM
(Service Control Manager) и по команде остановки корректно закрывает
сокеты, веб-панель и файлы экспорта. Логи — capture/service.log.

Реализация: StartServiceCtrlDispatcherW + RegisterServiceCtrlHandlerExW
напрямую через advapi32.dll, CreateServiceW для установки.
"""

import ctypes
import os
import sys
import threading
import time

SERVICE_NAME = "UniversalSniffer"
SERVICE_DISPLAY = "Universal Sniffer (TCP proxy + web panel)"
SERVICE_DESC = ("Универсальный TCP-прокси сниффер: перехват и разбор трафика, "
                "веб-панель, экспорт JSONL/SQLite/PCAP/CSV, тревоги.")

# --------------------------------------------------------------- константы SCM

SC_MANAGER_ALL_ACCESS = 0xF003F
SERVICE_ALL_ACCESS = 0xF01FF
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SERVICE_QUERY_STATUS = 0x0004
DELETE = 0x00010000

SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_AUTO_START = 0x00000002
SERVICE_ERROR_NORMAL = 0x00000001

SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004

SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000002

SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_SHUTDOWN = 0x00000002
SERVICE_CONTROL_INTERROGATE = 0x00000004

SERVICE_CONFIG_DESCRIPTION = 1
SERVICE_CONFIG_FAILURE_ACTIONS = 2
SC_ACTION_RESTART = 1

NO_ERROR = 0

ERROR_ACCESS_DENIED = 5
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_SERVICE_MARKED_FOR_DELETE = 1072
ERROR_SERVICE_EXISTS = 1073
ERROR_SERVICE_ALREADY_RUNNING = 1056


class ServiceError(Exception):
    """Понятная пользователю ошибка управления службой."""


# --------------------------------------------------------------- структуры

class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", ctypes.c_ulong),
        ("dwCurrentState", ctypes.c_ulong),
        ("dwControlsAccepted", ctypes.c_ulong),
        ("dwWin32ExitCode", ctypes.c_ulong),
        ("dwServiceSpecificExitCode", ctypes.c_ulong),
        ("dwCheckPoint", ctypes.c_ulong),
        ("dwWaitHint", ctypes.c_ulong),
    ]


class SERVICE_DESCRIPTIONW(ctypes.Structure):
    _fields_ = [("lpDescription", ctypes.c_wchar_p)]


class SC_ACTION(ctypes.Structure):
    _fields_ = [("Type", ctypes.c_uint), ("Delay", ctypes.c_uint)]


class SERVICE_FAILURE_ACTIONSW(ctypes.Structure):
    _fields_ = [
        ("dwResetPeriod", ctypes.c_ulong),
        ("lpRebootMsg", ctypes.c_wchar_p),
        ("lpCommand", ctypes.c_wchar_p),
        ("cActions", ctypes.c_ulong),
        ("lpsaActions", ctypes.POINTER(SC_ACTION)),
    ]


if os.name == "nt":
    LPSERVICE_MAIN_FUNCTIONW = ctypes.WINFUNCTYPE(
        None, ctypes.c_ulong, ctypes.POINTER(ctypes.c_wchar_p))
    LPHANDLER_FUNCTION_EX = ctypes.WINFUNCTYPE(
        ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)

    class SERVICE_TABLE_ENTRYW(ctypes.Structure):
        _fields_ = [("lpServiceName", ctypes.c_wchar_p),
                    ("lpServiceProc", LPSERVICE_MAIN_FUNCTIONW)]
else:  # заглушки для импорта на Linux/Android (реальные вызовы недоступны)
    LPSERVICE_MAIN_FUNCTIONW = None
    LPHANDLER_FUNCTION_EX = None
    SERVICE_TABLE_ENTRYW = None


# --------------------------------------------------------------- advapi32

_adv32 = None


def _dll():
    """Ленивая настройка advapi32 (только Windows)."""
    global _adv32
    if _adv32 is not None:
        return _adv32
    if os.name != "nt":
        raise ServiceError("Управление службой доступно только в Windows.")
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    HANDLE = ctypes.c_void_p

    def fn(name, rest, *args):
        f = getattr(adv, name)
        f.restype = rest
        f.argtypes = list(args)
        return f

    fn("OpenSCManagerW", HANDLE, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong)
    fn("CreateServiceW", HANDLE, HANDLE, ctypes.c_wchar_p, ctypes.c_wchar_p,
       ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
       ctypes.c_wchar_p, ctypes.c_wchar_p, HANDLE, ctypes.c_wchar_p,
       ctypes.c_wchar_p, ctypes.c_wchar_p)
    fn("OpenServiceW", HANDLE, HANDLE, ctypes.c_wchar_p, ctypes.c_ulong)
    fn("StartServiceW", ctypes.c_int, HANDLE, ctypes.c_ulong, ctypes.c_void_p)
    fn("ControlService", ctypes.c_int, HANDLE, ctypes.c_ulong,
       ctypes.POINTER(SERVICE_STATUS))
    fn("QueryServiceStatus", ctypes.c_int, HANDLE, ctypes.POINTER(SERVICE_STATUS))
    fn("DeleteService", ctypes.c_int, HANDLE)
    fn("CloseServiceHandle", ctypes.c_int, HANDLE)
    fn("ChangeServiceConfig2W", ctypes.c_int, HANDLE, ctypes.c_ulong, ctypes.c_void_p)
    fn("RegisterServiceCtrlHandlerExW", HANDLE, ctypes.c_wchar_p,
       LPHANDLER_FUNCTION_EX, ctypes.c_void_p)
    fn("SetServiceStatus", ctypes.c_int, HANDLE, ctypes.POINTER(SERVICE_STATUS))
    fn("StartServiceCtrlDispatcherW", ctypes.c_int, ctypes.POINTER(SERVICE_TABLE_ENTRYW))
    _adv32 = adv
    return adv


def _last_err():
    return ctypes.get_last_error()


def _raise(what, err=None):
    err = _last_err() if err is None else err
    if err == ERROR_ACCESS_DENIED:
        raise ServiceError("%s: нет прав — запустите от имени администратора." % what)
    raise ServiceError("%s: ошибка Windows %d" % (what, err))


def _open_scm():
    h = _dll().OpenSCManagerW(None, None, SC_MANAGER_ALL_ACCESS)
    if not h:
        _raise("OpenSCManager")
    return h


def _open_service(scm, access):
    h = _dll().OpenServiceW(scm, SERVICE_NAME, access)
    if not h:
        err = _last_err()
        if err == ERROR_SERVICE_DOES_NOT_EXIST:
            raise ServiceError("Служба не установлена (выполните --service install).")
        if err == ERROR_SERVICE_MARKED_FOR_DELETE:
            raise ServiceError("Служба помечена на удаление; перезагрузите компьютер.")
        _raise("OpenService", err)
    return h


def _query(h):
    ss = SERVICE_STATUS()
    if not _dll().QueryServiceStatus(h, ctypes.byref(ss)):
        _raise("QueryServiceStatus")
    return ss.dwCurrentState


def _wait_state(h, target, timeout=30.0):
    """Ждём выхода из PENDING-состояний в target."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _query(h)
        if st == target:
            return True
        if st not in (SERVICE_START_PENDING, SERVICE_STOP_PENDING):
            return st == target
        time.sleep(0.3)
    return False


# --------------------------------------------------------------- установка

def build_binpath(root, config_path):
    """Командная строка службы. В frozen-сборке (PyInstaller) — сам exe;
    в скриптовом режиме — python.exe + sniffer.py (для портативной сборки
    это runtime\\python.exe app\\sniffer.py). --root задаёт USNIFF_ROOT,
    чтобы служба находила config.json/capture/ независимо от рабочей папки."""
    if getattr(sys, "frozen", False):
        base = '"%s" --service run' % sys.executable
    else:
        script = os.path.abspath(sys.argv[0])
        base = '"%s" "%s" --service run' % (sys.executable, script)
    return '%s --root "%s" --config "%s"' % (base, root, config_path)


def install(root, config_path):
    """Создаёт службу (автозапуск) + описание + автоперезапуск при сбоях."""
    binpath = build_binpath(root, config_path)
    adv = _dll()
    scm = _open_scm()
    try:
        h = adv.CreateServiceW(scm, SERVICE_NAME, SERVICE_DISPLAY,
                               SERVICE_ALL_ACCESS, SERVICE_WIN32_OWN_PROCESS,
                               SERVICE_AUTO_START, SERVICE_ERROR_NORMAL,
                               binpath, None, None, None, None, None)
        if not h:
            err = _last_err()
            if err == ERROR_SERVICE_EXISTS:
                raise ServiceError("Служба уже установлена. Сначала: --service remove")
            _raise("CreateService", err)
        try:
            desc = SERVICE_DESCRIPTIONW(SERVICE_DESC)
            adv.ChangeServiceConfig2W(h, SERVICE_CONFIG_DESCRIPTION, ctypes.byref(desc))
            # автоперезапуск при сбоях: +5с, +30с, +60с; счётчик сброса — сутки
            actions = (SC_ACTION * 3)(SC_ACTION(SC_ACTION_RESTART, 5000),
                                      SC_ACTION(SC_ACTION_RESTART, 30000),
                                      SC_ACTION(SC_ACTION_RESTART, 60000))
            fa = SERVICE_FAILURE_ACTIONSW(86400, None, None, 3, actions)
            adv.ChangeServiceConfig2W(h, SERVICE_CONFIG_FAILURE_ACTIONS, ctypes.byref(fa))
        finally:
            adv.CloseServiceHandle(h)
    finally:
        adv.CloseServiceHandle(scm)
    return binpath


def remove():
    adv = _dll()
    scm = _open_scm()
    try:
        h = _open_service(scm, DELETE | SERVICE_STOP | SERVICE_QUERY_STATUS)
        try:
            st = _query(h)
            if st in (SERVICE_RUNNING, SERVICE_START_PENDING, SERVICE_STOP_PENDING):
                ss = SERVICE_STATUS()
                adv.ControlService(h, SERVICE_CONTROL_STOP, ctypes.byref(ss))
                _wait_state(h, SERVICE_STOPPED, timeout=20)
            if not adv.DeleteService(h):
                _raise("DeleteService")
        finally:
            adv.CloseServiceHandle(h)
    finally:
        adv.CloseServiceHandle(scm)


def start():
    adv = _dll()
    scm = _open_scm()
    try:
        h = _open_service(scm, SERVICE_START | SERVICE_QUERY_STATUS)
        try:
            if _query(h) == SERVICE_RUNNING:
                raise ServiceError("Служба уже запущена.")
            if not adv.StartServiceW(h, 0, None):
                err = _last_err()
                if err == ERROR_SERVICE_ALREADY_RUNNING:
                    raise ServiceError("Служба уже запущена.")
                _raise("StartService", err)
            if not _wait_state(h, SERVICE_RUNNING, timeout=30):
                raise ServiceError("Служба не перешла в состояние RUNNING (смотрите "
                                   "capture\\service.log и журнал Windows).")
        finally:
            adv.CloseServiceHandle(h)
    finally:
        adv.CloseServiceHandle(scm)


def stop():
    adv = _dll()
    scm = _open_scm()
    try:
        h = _open_service(scm, SERVICE_STOP | SERVICE_QUERY_STATUS)
        try:
            st = _query(h)
            if st == SERVICE_STOPPED:
                raise ServiceError("Служба уже остановлена.")
            ss = SERVICE_STATUS()
            if not adv.ControlService(h, SERVICE_CONTROL_STOP, ctypes.byref(ss)):
                err = _last_err()
                if err == ERROR_SERVICE_NOT_ACTIVE:
                    raise ServiceError("Служба уже остановлена.")
                _raise("ControlService", err)
            if not _wait_state(h, SERVICE_STOPPED, timeout=30):
                raise ServiceError("Служба не остановилась за 30 секунд.")
        finally:
            adv.CloseServiceHandle(h)
    finally:
        adv.CloseServiceHandle(scm)


def restart():
    try:
        stop()
        time.sleep(1.0)
    except ServiceError as e:
        if "уже остановлена" not in str(e) and "не установлена" not in str(e):
            raise
    start()


_STATUS_TEXT = {
    SERVICE_STOPPED: "остановлена",
    SERVICE_START_PENDING: "запускается...",
    SERVICE_STOP_PENDING: "останавливается...",
    SERVICE_RUNNING: "РАБОТАЕТ",
}


def status_text():
    """Человекочитаемое состояние службы."""
    if os.name != "nt":
        raise ServiceError("Управление службой доступно только в Windows.")
    adv = _dll()
    scm = _open_scm()
    try:
        h = adv.OpenServiceW(scm, SERVICE_NAME, SERVICE_QUERY_STATUS)
        if not h:
            return "Служба не установлена."
        try:
            return "Служба %s: %s." % (SERVICE_NAME, _STATUS_TEXT.get(_query(h), "?"))
        finally:
            adv.CloseServiceHandle(h)
    finally:
        adv.CloseServiceHandle(scm)


# --------------------------------------------------------------- режим run

_g = {"main_fn": None, "status_handle": None, "state": SERVICE_STOPPED,
      "stop_event": None, "svc_main": None, "handler": None}


def _report(state, accept=0, checkpoint=0, wait_hint=0):
    h = _g["status_handle"]
    if not h:
        return
    ss = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state, accept,
                        NO_ERROR, 0, checkpoint, wait_hint)
    _dll().SetServiceStatus(h, ctypes.byref(ss))
    _g["state"] = state


def _handler(control, event_type, event_data, context):
    """Обратный вызов SCM: STOP/SHUTDOWN -> мягкая остановка."""
    if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
        _report(SERVICE_STOP_PENDING, checkpoint=1, wait_hint=15000)
        ev = _g["stop_event"]
        if ev is not None:
            ev.set()
        return NO_ERROR
    if control == SERVICE_CONTROL_INTERROGATE:
        _report(_g["state"])
    return NO_ERROR


def _service_main(argc, argv):
    h = _dll().RegisterServiceCtrlHandlerExW(SERVICE_NAME, _g["handler"], None)
    if not h:
        return
    _g["status_handle"] = h
    _report(SERVICE_START_PENDING, checkpoint=1, wait_hint=10000)
    ev = threading.Event()
    _g["stop_event"] = ev
    worker = threading.Thread(target=_worker, args=(ev,), daemon=True,
                              name="sniffer-service")
    worker.start()
    while worker.is_alive():
        worker.join(timeout=1.0)
    _report(SERVICE_STOPPED)


def _worker(ev):
    try:
        _g["main_fn"](ev)
    except Exception:
        import traceback
        traceback.print_exc()


def run_service(main_fn):
    """Запуск в режиме службы (блокирует поток до остановки службы).
    main_fn(stop_event) — выполняет сниффер и возвращается при остановке.
    Бросает OSError, если процесс запущен не диспетчером служб."""
    _g["main_fn"] = main_fn
    svc_main = LPSERVICE_MAIN_FUNCTIONW(_service_main)
    handler = LPHANDLER_FUNCTION_EX(_handler)
    _g["svc_main"] = svc_main          # держим ссылки живыми
    _g["handler"] = handler
    table = (SERVICE_TABLE_ENTRYW * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = svc_main
    if not _dll().StartServiceCtrlDispatcherW(table):
        err = _last_err()
        raise OSError(err, "Процесс запущен не диспетчером служб (ошибка %d). "
                           "Для обычного запуска уберите --service run; для "
                           "фона используйте --service start." % err)
