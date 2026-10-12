"""Processjobb och publicerade generationer för crawlern (ingen nätverks-IO)."""
from __future__ import annotations

import asyncio
import ctypes
from contextlib import closing
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid


def contained_path(root: str, path: str) -> str:
    """Verifiera ett absolut mål före flytt eller rekursiv borttagning."""
    base, target = os.path.realpath(root), os.path.realpath(path)
    if os.path.commonpath([base, target]) != base or target == base:
        raise ValueError('Arbetsfilens sökväg lämnar utmappen')
    return target


def remove_tree(root: str, path: str):
    target = contained_path(root, path)
    if os.path.exists(target):
        shutil.rmtree(target)


def write_json(path: str, value, retry_replace: bool = False):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    deadline = time.monotonic() + (2 if retry_replace else 0)
    while True:
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            # På Windows kan en läsare tillfälligt hålla pointer-filen öppen.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


async def blocking_complete(function, *args, **kwargs):
    """Avbrutna trådjobb måste slutföras innan deras arbetsmapp får städas."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class WindowsProcessJob:
    """Stängning av job-handle avslutar också workerprocessens barnprocesser."""
    def __init__(self, pid: int):
        from ctypes import wintypes as wt

        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', wt.DWORD), ('min_working_set', ctypes.c_size_t),
                        ('max_working_set', ctypes.c_size_t), ('active_processes', wt.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wt.DWORD), ('scheduling', wt.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', IoCounters),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        kernel.CreateJobObjectW.restype = wt.HANDLE
        kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        kernel.SetInformationJobObject.restype = wt.BOOL
        kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel.OpenProcess.restype = wt.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        kernel.AssignProcessToJobObject.restype = wt.BOOL
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        kernel.CloseHandle.restype = wt.BOOL
        self.kernel, self.handle = kernel, kernel.CreateJobObjectW(None, None)
        process = None
        try:
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ConversionProcessRunner:
    """En process per dokument, begränsad samtidighet och en verklig avslutningsbar timeout."""
    def __init__(self, work_root: str, workers: int = 2, timeout: float = 180,
                 worker_script: str | None = None):
        self.work_root = os.path.abspath(work_root)
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(workers)
        self.worker_script = worker_script or str(Path(__file__).with_name('conversion_worker.py'))
        self.active = set()
        self.closed = False

    def command(self, job_path: str, result_path: str) -> list[str]:
        if getattr(sys, 'frozen', False):
            return [sys.executable, '--conversion-worker', job_path, result_path]
        return [sys.executable, '-u', self.worker_script, job_path, result_path]

    @staticmethod
    async def _reap(process, job):
        if job:
            job.close()
        elif os.name != 'nt':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    async def run(self, filepath: str, url: str, ref: dict, options: dict,
                  md_filename: str, max_bytes: int, expected_hash: str | None = None) -> dict:
        async with self.semaphore:
            if self.closed:
                return {'text': None, 'ocr': False, 'error': 'Konverteringskön är stängd'}
            os.makedirs(self.work_root, exist_ok=True)
            directory = tempfile.mkdtemp(prefix='job-', dir=self.work_root)
            process, job = None, None
            try:
                input_dir = os.path.join(directory, 'input')
                os.makedirs(input_dir)
                input_path = os.path.join(input_dir, os.path.basename(filepath))
                # Parsern får en egen kopia och kan inte råka skriva i en publicerad hårdlänk.
                copy_task = asyncio.create_task(asyncio.to_thread(shutil.copyfile, filepath, input_path))
                try:
                    await asyncio.shield(copy_task)
                except asyncio.CancelledError:
                    await copy_task
                    raise
                request = {'input': input_path, 'url': url, 'ref': ref, 'options': options,
                           'output': os.path.join(directory, 'output'), 'expected_hash': expected_hash}
                job_path, result_path = os.path.join(directory, 'job.json'), os.path.join(directory, 'result.json')
                request['go_file'] = os.path.join(directory, 'go')
                write_json(job_path, request)
                environment = dict(os.environ)
                environment.pop('SSLKEYLOGFILE', None)
                for name in ('TEMP', 'TMP', 'TMPDIR'):
                    environment[name] = directory
                kwargs = ({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt'
                          else {'start_new_session': True})
                process = await asyncio.create_subprocess_exec(
                    *self.command(job_path, result_path),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, env=environment, **kwargs)
                self.active.add(process)
                if os.name == 'nt':
                    job = WindowsProcessJob(process.pid)
                # Workern väntar på starttecknet tills processgruppen/jobbet skyddats.
                write_json(request['go_file'], True)
                # Textbaserade test-/verktygsworkers kan också använda stdin.
                # pythonw/fönsterlösa exe-filer har inte alltid sys.stdin.
                try:
                    process.stdin.write(b'G')
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.timeout)
                except asyncio.TimeoutError:
                    return {'text': None, 'ocr': False,
                            'error': f'Dokumentkonverteringen överskred tidsgränsen ({self.timeout:g} s)'}
                if process.returncode != 0:
                    return {'text': None, 'ocr': False,
                            'error': f'Dokumentprocessen avslutades oväntat (exit {process.returncode})'}
                if os.path.getsize(result_path) > 65536:
                    raise ValueError('För stort konverteringsresultat')
                with open(result_path, encoding='utf-8') as result_file:
                    result = json.load(result_file)
                error = str(result.get('error') or '')[:2000]
                if not result.get('path'):
                    return {'text': None, 'ocr': bool(result.get('ocr')), 'error': error}
                expected_path = os.path.join(directory, 'output', 'texter', md_filename)
                if contained_path(directory, result['path']) != os.path.realpath(expected_path):
                    raise ValueError('Workern returnerade fel utdatafil')
                if not 0 < os.path.getsize(expected_path) <= max_bytes:
                    raise ValueError('Den konverterade texten överskrider storleksgränsen')
                with open(expected_path, encoding='utf-8') as output:
                    text = output.read()
                return {'text': text, 'ocr': bool(result.get('ocr')), 'error': error}
            except asyncio.CancelledError:
                raise
            except Exception as error:
                return {'text': None, 'ocr': False, 'error': f'Konverteringsjobb misslyckades: {error}'}
            finally:
                if process:
                    await self._reap(process, job)
                    self.active.discard(process)
                await asyncio.to_thread(remove_tree, self.work_root, directory)

    async def close(self):
        self.closed = True
        # Aktiva run-anrop ska avbrytas/awaitas av kön innan dess arbetsmapp städas.
        for process in list(self.active):
            if process.returncode is None:
                process.kill()


class PublicationWorkspace:
    """Förbered en hel generation och byt endast current.json vid publicering.

    Utmapparnas äldre toppnivåfiler speglas efteråt för kompatibilitet. Läsare
    som behöver en sammanhängande leverans måste pinna generationen från current.json.
    """
    DIRECTORIES = ('texter', 'texter_karantan', 'dokument', 'texter_utgangna')

    def __init__(self, root: str, cache_name: str, run: str):
        self.root = os.path.abspath(root)
        self.cache_name, self.run = cache_name, run
        self.generation_id = uuid.uuid4().hex
        self.workdir = os.path.join(self.root, '.working', self.generation_id)
        self.generation_dir = os.path.join(self.root, '.generations', self.generation_id)
        self.lock = None
        self.published = False
        self.mirror_errors = []
        self.previous_directory = None

    def _acquire_lock(self):
        self.lock = open(os.path.join(self.root, '.crawl.lock'), 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.lock.seek(0, os.SEEK_END)
                if self.lock.tell() == 0:
                    self.lock.write(b'\0')
                    self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.lock.close()
            self.lock = None
            raise RuntimeError('En annan crawl använder redan samma utmapp')

    def current(self) -> dict | None:
        path = os.path.join(self.root, 'current.json')
        if not os.path.exists(path):
            return None
        with open(path, encoding='utf-8') as pointer_file:
            pointer = json.load(pointer_file)
        generation = pointer.get('generation', '')
        if (not isinstance(generation, str) or not generation.startswith('.generations/')
                or len(generation.split('/')) != 2 or len(generation.split('/')[1]) != 32
                or any(char not in '0123456789abcdef' for char in generation.split('/')[1])):
            raise ValueError('current.json har en ogiltig generation')
        folder = contained_path(self.root, os.path.join(self.root, generation))
        if not os.path.isdir(folder):
            raise ValueError('Den publicerade generationen saknas')
        return pointer

    @staticmethod
    def _link_or_copy(source, target):
        if os.path.islink(source):
            raise ValueError('Symboliska länkar stöds inte i publicerade utdata')
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)

    def _artifact_names(self, source):
        names = ['index.csv', 'chunks.jsonl', 'chunks_quarantine.jsonl', 'changes.jsonl', 'crawl_report.json']
        names.extend(name for name in os.listdir(source) if name.startswith('manifest_') and name.endswith('.json'))
        return [name for name in names if os.path.isfile(os.path.join(source, name))]

    def begin(self):
        os.makedirs(self.root, exist_ok=True)
        self._acquire_lock()
        try:
            working_root = os.path.join(self.root, '.working')
            if os.path.isdir(working_root):
                for name in os.listdir(working_root):
                    if len(name) == 32 and all(char in '0123456789abcdef' for char in name):
                        stale = os.path.join(working_root, name)
                        if os.path.isdir(stale):
                            remove_tree(self.root, stale)
            pointer = self.current()
            source = os.path.join(self.root, pointer['generation']) if pointer else self.root
            self.previous_directory = source if pointer else None
            contained_path(self.root, self.workdir)
            os.makedirs(self.workdir)
            for folder in self.DIRECTORIES:
                source_dir = os.path.join(source, folder)
                if os.path.isdir(source_dir):
                    shutil.copytree(source_dir, os.path.join(self.workdir, folder),
                                    copy_function=self._link_or_copy,
                                    ignore=shutil.ignore_patterns('*.part', '*.tmp'))
            # Nytillagda manuella textfiler ska arkiveras och rapporteras som förr.
            # Befintliga kompatibilitetsfiler får däremot inte ändra en generation.
            if pointer:
                for folder in ('texter', 'texter_karantan'):
                    legacy = os.path.join(self.root, folder)
                    target_dir = os.path.join(self.workdir, folder)
                    if not os.path.isdir(legacy):
                        continue
                    os.makedirs(target_dir, exist_ok=True)
                    for name in os.listdir(legacy):
                        source_file, target_file = os.path.join(legacy, name), os.path.join(target_dir, name)
                        if name.endswith(('.md', '.txt', '.json')) and os.path.isfile(source_file) and not os.path.exists(target_file):
                            shutil.copy2(source_file, target_file)
            for name in self._artifact_names(source):
                self._link_or_copy(os.path.join(source, name), os.path.join(self.workdir, name))
            source_cache = os.path.join(source, self.cache_name)
            if os.path.isfile(source_cache):
                with closing(sqlite3.connect(Path(source_cache).as_uri() + '?mode=ro', uri=True)) as old:
                    with closing(sqlite3.connect(os.path.join(self.workdir, self.cache_name))) as new:
                        old.backup(new)
            return self.workdir
        except BaseException:
            self.abort()
            raise

    def _atomic_copy(self, source, target):
        contained_path(self.root, target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + '.publish.tmp'
        if os.path.exists(tmp):
            os.remove(tmp)
        # Äldre integrationer kan redigera dessa filer; dela därför aldrig deras
        # inode med den oföränderliga generationen.
        shutil.copy2(source, tmp)
        os.replace(tmp, target)

    def _unchanged_legacy_copy(self, source, target) -> bool:
        if not self.previous_directory or not os.path.isfile(target):
            return False
        previous = os.path.join(self.previous_directory, os.path.relpath(source, self.generation_dir))
        if not os.path.isfile(previous) or not os.path.samefile(previous, source):
            return False
        old_stat, copy_stat = os.stat(source), os.stat(target)
        return old_stat.st_size == copy_stat.st_size and old_stat.st_mtime_ns == copy_stat.st_mtime_ns

    def mirror(self):
        for folder in self.DIRECTORIES:
            source_dir, target_dir = os.path.join(self.generation_dir, folder), os.path.join(self.root, folder)
            expected = set()
            if os.path.isdir(source_dir):
                for base, _, names in os.walk(source_dir):
                    for name in names:
                        relative = os.path.relpath(os.path.join(base, name), source_dir)
                        expected.add(relative)
                        source_file, target_file = os.path.join(base, name), os.path.join(target_dir, relative)
                        if not self._unchanged_legacy_copy(source_file, target_file):
                            self._atomic_copy(source_file, target_file)
            if os.path.isdir(target_dir):
                for base, _, names in os.walk(target_dir):
                    for name in names:
                        path = contained_path(self.root, os.path.join(base, name))
                        if os.path.relpath(path, target_dir) not in expected:
                            os.remove(path)
        names = self._artifact_names(self.generation_dir) + [self.cache_name]
        for name in names:
            if os.path.isfile(os.path.join(self.generation_dir, name)):
                self._atomic_copy(os.path.join(self.generation_dir, name), os.path.join(self.root, name))
        for name in self._artifact_names(self.root):
            if name not in names and name != 'crawl_report.json':
                os.remove(contained_path(self.root, os.path.join(self.root, name)))

    def publish(self, status: str):
        if status not in ('success', 'partial'):
            raise ValueError('En misslyckad körning får inte publiceras')
        contained_path(self.root, self.generation_dir)
        os.makedirs(os.path.dirname(self.generation_dir), exist_ok=True)
        # DB-anslutningar och dokumentprocesser ska redan vara avslutade.
        for name in os.listdir(self.workdir):
            if name.startswith('.conversion'):
                remove_tree(self.workdir, os.path.join(self.workdir, name))
        artifacts = []
        for base, _, names in os.walk(self.workdir):
            for name in sorted(names):
                path = os.path.join(base, name)
                if name.endswith(('.tmp', '.part', '-wal', '-shm')):
                    raise ValueError('Ofärdiga arbetsfiler får inte publiceras')
                relative = os.path.relpath(path, self.workdir).replace(os.sep, '/')
                if not relative.startswith('texter_utgangna/') and relative != self.cache_name:
                    artifacts.append(relative)
        pointer = {'schema': 1, 'generation': '.generations/' + self.generation_id,
                   'run': self.run, 'status': status, 'artifacts': sorted(artifacts),
                   'corpus': 'chunks.jsonl' if os.path.isfile(os.path.join(self.workdir, 'chunks.jsonl')) else None,
                   'quarantine': 'chunks_quarantine.jsonl' if os.path.isfile(
                       os.path.join(self.workdir, 'chunks_quarantine.jsonl')) else None}
        write_json(os.path.join(self.workdir, 'generation.json'), pointer)
        os.replace(self.workdir, self.generation_dir)
        write_json(os.path.join(self.root, 'current.json'), pointer, retry_replace=True)
        self.published = True
        try:
            self.mirror()
        except Exception as error:
            self.mirror_errors.append(str(error))
        return pointer

    def prune(self, keep: int, min_age_hours: float) -> list[str]:
        """Valbar historikstädning under skrivlåset, med respit efter pensionering.

        Antalet är ett mål: generationer inom respiten behålls för pinnade läsare.
        Den aktuella generationen och okända mappar får aldrig tas bort.
        """
        if keep <= 0:
            return []
        pointer = self.current()
        if not pointer:
            return []
        current = pointer['generation'].split('/')[1]
        state_path = os.path.join(self.root, '.generation_retirements.json')
        retired = {}
        if os.path.isfile(state_path):
            with open(state_path, encoding='utf-8') as state_file:
                retired = json.load(state_file)
            if not isinstance(retired, dict):
                raise ValueError('Ogiltig historik för generationsstädning')
        now = time.time()
        root = os.path.join(self.root, '.generations')
        if os.path.normcase(os.path.realpath(root)) != os.path.normcase(os.path.abspath(root)):
            raise ValueError('Generationsstädning får inte följa mappalias')
        generations = []
        for name in os.listdir(root):
            if len(name) != 32 or any(char not in '0123456789abcdef' for char in name):
                continue
            candidate = os.path.join(root, name)
            # Ett alias/junction till aktuell generation får aldrig kunna bli
            # ett separat, oskyddat städmål med ett annat generationsnamn.
            if (os.path.islink(candidate)
                    or getattr(os.lstat(candidate), 'st_file_attributes', 0) & 0x400
                    or os.path.normcase(os.path.realpath(candidate)) != os.path.normcase(os.path.abspath(candidate))):
                continue
            folder = contained_path(self.root, candidate)
            marker = os.path.join(folder, 'generation.json')
            if not os.path.isdir(folder) or not os.path.isfile(marker):
                continue
            generations.append((os.path.getmtime(marker), name, folder))
            if name == current:
                retired.pop(name, None)
            elif name not in retired:
                retired[name] = now
        latest = [name for _, name, _ in sorted(generations, reverse=True) if name != current]
        protected = {current, *latest[:max(0, keep - 1)]}
        removed = []
        for _, name, folder in generations:
            stamp = retired.get(name, now)
            if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp > now:
                raise ValueError('Ogiltig pensioneringstid för en generation')
            if name not in protected and now - stamp >= min_age_hours * 3600:
                remove_tree(self.root, folder)
                retired.pop(name, None)
                removed.append(name)
        write_json(state_path, retired)
        return removed

    def abort(self):
        if os.path.exists(self.workdir):
            remove_tree(self.root, self.workdir)
        # En generation som flyttats men inte publicerats kan tryggt lämnas som
        # historik; den får aldrig väljas genom något annat än current.json.
        self.release()

    def release(self):
        if self.lock:
            self.lock.close()  # OS-låset släpps också automatiskt efter en processkrasch.
            self.lock = None
