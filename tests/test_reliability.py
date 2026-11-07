import base64
from io import BytesIO
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from emulator_agent.controller import Controller
from emulator_agent.discovery import ControlError, Instance, Registry, run_command


class ReliabilityTests(unittest.TestCase):
    def controller(self):
        with patch('emulator_agent.controller.Registry'):
            return Controller(Path('.'))

    def registry(self):
        r = Registry.__new__(Registry)
        r.roots = {'ld': Path('ld'), 'mumu': Path('mumu')}
        r.query_restricted = False
        return r

    def test_target_lookup_does_not_query_other_vendor(self):
        r = self.registry()
        with patch('emulator_agent.discovery.run_command', return_value='{"index":0,"is_android_started":true}') as command:
            self.assertEqual(r.get('mumu:0').id, 'mumu:0')
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0].name, 'MuMuManager.exe')

    def test_vendor_failure_is_reported_alongside_healthy_instances(self):
        c = self.controller()
        c.registry = self.registry()
        def query(executable, *args):
            if executable.name == 'ldconsole.exe':
                raise ControlError('LD unavailable')
            return '{"index":0,"is_android_started":true,"pid":12,"render_wnd":"33"}'
        with patch('emulator_agent.discovery.run_command', side_effect=query):
            result = c.list_instances()
        self.assertEqual(result[0]['state'], 'query_failed')
        self.assertIsNone(result[0]['running'])
        self.assertEqual(result[0]['error'], 'LD unavailable')
        self.assertEqual(result[1]['instance_id'], 'mumu:0')

    def test_state_does_not_treat_restricted_or_conflicting_query_as_stopped(self):
        for kwargs, expected in [({}, 'stopped'), ({'pid': 12}, 'unknown'),
                                 ({'query_restricted': True}, 'unknown'),
                                 ({'running': True}, 'starting'),
                                 ({'running': True, 'pid': 12, 'render_hwnd': 34}, 'running')]:
            with self.subTest(kwargs=kwargs):
                args = {'running': False, **kwargs}
                instance = Instance('mumu', 0, 'test', '.', **args)
                self.assertEqual(instance.public()['state'], expected)
                self.assertNotIn('root', instance.public())

    def test_malformed_mumu_records_do_not_hide_healthy_ld_instances(self):
        c = self.controller()
        c.registry = self.registry()
        malformed = ['{"index":"bad"}', '{"index":0,"pid":null}',
                     '{"index":0,"main_wnd":"not-hex"}', '{"index":0,"is_android_started":"false"}',
                     '[null]', 'null', '"bad"', '{"error_code":2}', '{"index":true}', '{"index":1.9}',
                     '{"index":0,"pid":true}', '{"index":0,"pid":1.9}',
                     '{"index":0,"main_wnd":false}', '{"index":0,"render_wnd":1.5}',
                     '{"index":-1}', '{"index":0,"pid":-1}', '{"index":0,"render_wnd":-1}']
        for payload in malformed:
            with self.subTest(payload=payload):
                def query(executable, *args):
                    return '0,LD,0,0,0,0,0' if executable.name == 'ldconsole.exe' else payload
                with patch('emulator_agent.discovery.run_command', side_effect=query):
                    result = c.list_instances()
                self.assertEqual(result[0]['instance_id'], 'ld:0')
                self.assertEqual(result[1]['state'], 'query_failed')
                self.assertTrue(result[1]['error'])

    def test_uncertain_state_blocks_native_input_and_lifecycle_dispatch(self):
        c = self.controller()
        c.registry = self.registry()
        for instance in (Instance('mumu', 0, 'test', '.', False, pid=12),
                         Instance('mumu', 0, 'test', '.', False, query_restricted=True)):
            c.registry.get = Mock(return_value=instance)
            with patch('emulator_agent.controller.Worker') as worker, patch('emulator_agent.discovery.run_command') as command:
                with self.assertRaisesRegex(ControlError, '无法确认'):
                    c.screenshot('mumu:0')
                for state in ('start', 'stop', 'restart'):
                    with self.assertRaisesRegex(ControlError, '无法确认'):
                        c.set_instance_state('mumu:0', state)
                worker.assert_not_called()
                command.assert_not_called()

    def test_cancel_removes_queued_stop_and_restart(self):
        for state in ('stop', 'restart'):
            with self.subTest(state=state):
                c = self.controller()
                slot = c.slot('mumu:0')
                queued = threading.Event()
                errors = []
                original_cancel = c._cancel
                def track_cancel(target):
                    result = original_cancel(target)
                    queued.set()
                    return result
                def request():
                    try:
                        c.set_instance_state('mumu:0', state)
                    except Exception as exc:
                        errors.append(exc)
                with patch.object(c, '_cancel', side_effect=track_cancel):
                    with slot.lock:
                        thread = threading.Thread(target=request, daemon=True)
                        thread.start()
                        self.assertTrue(queued.wait(2))
                        c.cancel('mumu:0')
                    thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], ControlError)
                c.registry.lifecycle.assert_not_called()

    def test_client_cancellation_before_thread_start_blocks_side_effects(self):
        c = self.controller()
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(ControlError):
            c.set_instance_state('mumu:0', 'stop', cancelled=cancelled)
        with self.assertRaises(ControlError):
            c.action('mumu:0', 'press_key', key='home', cancelled=cancelled)
        c.registry.get.assert_not_called()

    def test_cancel_during_validation_blocks_management_command(self):
        c = self.controller()
        c.registry = self.registry()
        def lookup(_):
            c.cancel('mumu:0')
            return Instance('mumu', 0, 'test', '.', True, 12, 34, 56)
        c.registry.get = lookup
        with patch('emulator_agent.discovery.run_command') as command:
            with self.assertRaisesRegex(ControlError, '取消'):
                c.set_instance_state('mumu:0', 'stop')
            command.assert_not_called()

    def test_restart_cancel_after_shutdown_does_not_launch(self):
        r = self.registry()
        cancelled = threading.Event()
        instance = Instance('mumu', 0, 'test', '.', True, 12, 34, 56)
        def shutdown(*args, **kwargs):
            cancelled.set()
            return ''
        with patch('emulator_agent.discovery.run_command', side_effect=shutdown) as command:
            with self.assertRaisesRegex(ControlError, '取消'):
                r.lifecycle(instance, 'restart', is_cancelled=cancelled.is_set)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[-1], 'shutdown')

    def test_cancellation_is_checked_inside_command_dispatch_lock(self):
        cancelled = threading.Event()
        class Gate:
            def __enter__(self):
                cancelled.set()
            def __exit__(self, *args):
                pass
        with patch('emulator_agent.discovery.subprocess.Popen') as spawn:
            with self.assertRaises(ControlError):
                run_command(Path('fake.exe'), start_lock=Gate(), is_cancelled=cancelled.is_set)
            spawn.assert_not_called()

    def test_timed_out_command_reaps_only_its_own_process(self):
        process = Mock()
        process.__enter__ = Mock(return_value=process)
        process.__exit__ = Mock(return_value=False)
        process.communicate.side_effect = [subprocess.TimeoutExpired('fake', 1), (b'', b'')]
        with patch('emulator_agent.discovery.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(ControlError, '超时'):
                run_command(Path('fake.exe'), timeout=1)
        process.kill.assert_called_once_with()
        self.assertEqual(process.communicate.call_count, 2)

    def test_log_failure_preserves_input_result_screenshot_and_original_error(self):
        c = self.controller()
        slot = c.slot('mumu:0')
        slot.worker = Mock()
        c.worker = Mock(return_value=slot.worker)
        slot.worker.request.side_effect = [{'status': 'input_sent', 'task_verified': False},
                                           {'status': 'captured'}, ControlError('original input outcome unknown')]
        with patch.object(Path, 'mkdir', side_effect=PermissionError('no logging')):
            result = c.action('mumu:0', 'press_key', key='home')
            self.assertEqual(result['status'], 'input_sent')
            self.assertFalse(result['task_verified'])
            self.assertTrue(result['warnings'])
            result = c.screenshot('mumu:0')
            self.assertEqual(result['status'], 'captured')
            self.assertTrue(result['warnings'])
            with self.assertRaisesRegex(ControlError, 'original input outcome unknown'):
                c.action('mumu:0', 'press_key', key='home')

    def test_log_file_failure_preserves_lifecycle_result(self):
        c = self.controller()
        c.registry.lifecycle.return_value = Instance('mumu', 0, 'test', '.', False)
        with patch.object(Path, 'mkdir'), patch.object(Path, 'open', side_effect=OSError('disk full')):
            result = c.set_instance_state('mumu:0', 'stop')
        self.assertEqual(result['state'], 'stopped')
        self.assertTrue(result['warnings'])

    def test_full_frame_reuses_png_and_transformed_frame_maps_coordinates(self):
        c = self.controller()
        slot = c.slot('mumu:0')
        slot.worker = Mock()
        slot.worker.instance = Instance('mumu', 0, 'test', '.', True, 12, 34, 56)
        source = BytesIO()
        Image.new('RGB', (1000, 2000), 'red').save(source, 'PNG')
        png = source.getvalue()
        def frame():
            return {'status': 'captured', 'width': 1000, 'height': 2000,
                    'png': base64.b64encode(png).decode('ascii')}
        with patch('emulator_agent.controller.Image.open', side_effect=AssertionError('PNG decoded again')):
            result = c.render_frame(slot, 'mumu:0', frame(), max_size=4096)
            self.assertEqual(result['image_bytes'], png)
        result = c.render_frame(slot, 'mumu:0', frame(), crop=[100, 200, 500, 1000], max_size=400)
        self.assertEqual((result['width'], result['height']), (200, 400))
        self.assertEqual(slot.frames[result['frame_id']]['box'], (100, 200, 500, 1000))
        with Image.open(BytesIO(result['image_bytes'])) as image:
            self.assertEqual(image.size, (200, 400))


if __name__ == '__main__':
    unittest.main()
