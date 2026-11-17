from collections import OrderedDict
from io import BytesIO
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from emulator_agent.controller import Controller, Slot, integer
from emulator_agent.discovery import ControlError, Instance, parse_ld, parse_mumu, run_command
from emulator_agent.workers import MAX_RESPONSE_BYTES, Worker


class CoreTests(unittest.TestCase):
    def test_ld_key_targets_render_window_and_includes_scan_code(self):
        from emulator_agent.native import LDNative
        native = LDNative.__new__(LDNative)
        native.instance = Instance('ld', 0, 'test', '.', True, 12, 34, 56)
        native.validate_input_window = Mock()
        with patch('emulator_agent.native.user') as user, patch('emulator_agent.native.post') as post:
            user.MapVirtualKeyW.return_value = 0x3B
            native.key('home')
        native.validate_input_window.assert_called_once()
        self.assertEqual(post.call_args_list[0].args, (56, 0x100, 0x70, 0x003B0001))
        self.assertEqual(post.call_args_list[1].args, (56, 0x101, 0x70, 0xC03B0001))

    def test_ld_unavailable_keys_do_not_send_input(self):
        from emulator_agent.native import LDNative
        native = LDNative.__new__(LDNative)
        with patch('emulator_agent.native.post') as post:
            for key in ('back', 'enter'):
                with self.assertRaises(ControlError):
                    native.key(key)
            post.assert_not_called()

    def test_ld_text_uses_official_command_with_exact_argument(self):
        from emulator_agent.native import LDNative
        native = LDNative.__new__(LDNative)
        native.instance = Instance('ld', 2, 'test', 'C:/fake emulator', True, 12, 34, 56)
        native.validate_input_window = Mock()
        process = Mock()
        process.__enter__ = Mock(return_value=process)
        process.__exit__ = Mock(return_value=False)
        process.communicate.return_value = (b'', b'')
        process.returncode = 0
        value = r'测试 "quoted" & 100% \ path'
        with patch('emulator_agent.discovery.subprocess.Popen', return_value=process) as spawn:
            native.text(value)
        native.validate_input_window.assert_called_once()
        self.assertEqual(spawn.call_args.args[0], [str(Path(native.instance.root) / 'ldconsole.exe'),
                         'action', '--index', '2', '--key', 'call.input', '--value', value])
        self.assertFalse(spawn.call_args.kwargs.get('shell', False))
        process.communicate.assert_called_once_with(timeout=8)

    def test_windows_process_passes_special_text_without_shell(self):
        value = r'测试 "quoted" & 100% \ path'
        code = 'import json,sys; print(json.dumps(sys.argv[1:]))'
        output = run_command(Path(sys.executable), '-c', code, value)
        self.assertEqual(json.loads(output), [value])

    def test_ld_text_action_returns_new_frame_and_unverified_status(self):
        from emulator_agent.native import LDNative
        native = LDNative.__new__(LDNative)
        native.instance = Instance('ld', 0, 'test', '.', True, 12, 34, 56)
        native.validate_input_window = Mock()
        native.screenshot = Mock(return_value=Image.new('RGB', (2, 2)))
        cancelled = Mock()
        cancelled.is_set.return_value = False
        with patch('emulator_agent.native.run_command') as command:
            result = native.execute({'op': 'input_text', 'text': 'example'}, cancelled)
        command.assert_called_once()
        self.assertEqual(native.screenshot.call_count, 2)
        self.assertEqual(result['status'], 'input_sent')
        self.assertFalse(result['task_verified'])
        self.assertIn('png', result)

    def test_ld_text_failure_does_not_retry_or_expose_value(self):
        from emulator_agent.native import LDNative
        native = LDNative.__new__(LDNative)
        native.instance = Instance('ld', 0, 'test', '.', True, 12, 34, 56)
        native.validate_input_window = Mock()
        with patch('emulator_agent.native.run_command', side_effect=ControlError('timeout')) as command:
            with self.assertRaisesRegex(ControlError, '结果可能未知') as error:
                native.text('private-example')
        command.assert_called_once()
        self.assertNotIn('private-example', str(error.exception))

    def test_ld_text_refuses_invalid_window_before_dispatch(self):
        from emulator_agent.native import LDNative
        native = LDNative.__new__(LDNative)
        native.validate_input_window = Mock(side_effect=ControlError('invalid window'))
        with patch('emulator_agent.native.run_command') as command:
            with self.assertRaises(ControlError):
                native.text('example')
        command.assert_not_called()

    def test_text_capability_for_both_vendors_and_mumu_utf8(self):
        from emulator_agent.native import MuMuNative
        c = Controller.__new__(Controller)
        c.registry = Mock()
        for vendor in ('ld', 'mumu'):
            with self.subTest(vendor=vendor):
                c.registry.get.return_value = Instance(vendor, 0, 'test', '.', False)
                self.assertTrue(c.get_instance(f'{vendor}:0')['capabilities']['input_text'])
        native = MuMuNative.__new__(MuMuNative)
        native.checked = Mock()
        native.text('测试 A')
        value = '测试 A'.encode('utf-8')
        native.checked.assert_called_once_with('nemu_input_text', len(value), value)

    def test_protocol_limit_covers_maximum_native_frame(self):
        self.assertGreater(MAX_RESPONSE_BYTES, (24_000_000 * 3 + 1_000_000) * 4 // 3)

    def test_ld_parser_preserves_unicode_and_csv_name(self):
        item = parse_ld('2,"雷电,工作",11,22,1,333,444,900,1600,320', Path('.'))[0]
        self.assertEqual((item.id,item.name,item.generation,item.width),('ld:2','雷电,工作',(333,11,22),900))
        with self.assertRaises(ControlError):parse_ld('0,bad,1',Path('.'))

    def test_mumu_multiple_json_records_and_hex_handles(self):
        items=parse_mumu('{"index":"0","name":"中文","is_android_started":true,"main_wnd":"AB","render_wnd":"10","pid":2}\n{"index":"1","is_android_started":false}',Path('.'))
        self.assertEqual((len(items),items[0].hwnd,items[0].render_hwnd,items[1].running),(2,171,16,False))

    def test_integer_rejects_bool_and_out_of_bounds(self):
        for value in (True,-1,10,1.5):
            with self.assertRaises(ControlError):integer(value,'x',0,9)

    def test_closing_rejects_lifecycle_request(self):
        c = Controller.__new__(Controller)
        c.slots = {}; c.map_lock = threading.Lock()
        c.closing = threading.Event(); c.closing.set()
        c.registry = Mock()
        with self.assertRaises(ControlError):
            c.set_instance_state('ld:0', 'start')
        c.registry.lifecycle.assert_not_called()

    def controller_stub(self):
        c=Controller.__new__(Controller)
        c.slots={};c.map_lock=threading.Lock()
        c.log=Mock(return_value=None)
        c.closing=threading.Event()
        instance=Instance('mumu',0,'test','.',True,12,34,56)
        w=Mock();w.instance=instance
        c.worker=Mock(return_value=w)
        c.slot('mumu:0').worker=w
        w.request.return_value={'status':'input_sent'}
        return c,w

    def test_crop_and_resize_coordinate_conversion(self):
        c,w=self.controller_stub();s=c.slot('mumu:0')
        s.frames['f']={'generation':w.instance.generation,'size':(1000,2000),'box':(100,200,500,1000),'view_size':(200,400)}
        c.action('mumu:0','click',frame_id='f',points=[[100,200]])
        request=w.request.call_args.args[0]
        self.assertEqual(request['points'],[[300,600]])
        self.assertEqual(request['expected_size'],[1000,2000])
        self.assertFalse(s.frames)

    def test_cross_instance_or_expired_frame_is_rejected(self):
        c,w=self.controller_stub()
        with self.assertRaises(ControlError):c.action('mumu:0','click',frame_id='wrong',points=[[1,1]])
        w.request.assert_not_called()

    def test_restart_invalidates_frame(self):
        c,w=self.controller_stub();s=c.slot('mumu:0')
        s.frames['f']={'generation':(1,2,3)}
        with self.assertRaises(ControlError):c.action('mumu:0','click',frame_id='f',points=[[1,1]])
        w.request.assert_not_called()

    def test_cancel_between_validation_and_dispatch_is_not_lost(self):
        w=Worker.__new__(Worker);w.lock=threading.Lock();w.active=threading.Event()
        w.process=Mock();w.process.poll.return_value=None;w.send=Mock()
        with self.assertRaises(ControlError):w.request({'op':'click'},threading.Lock(),lambda:True)
        w.send.assert_not_called()
        self.assertFalse(w.active.is_set())

    def test_native_cancellation_releases_touch(self):
        from emulator_agent.native import Native
        native=Mock(spec=Native)
        native.width=native.height=100
        native.screenshot.return_value=Image.new('RGB',(100,100))
        event=threading.Event()
        def touch(*_):event.set()
        native.touch.side_effect=touch
        with self.assertRaises(ControlError):
            Native.execute(native,{'op':'long_press','points':[[1,1]],'duration':2},event)
        self.assertTrue(native.release.called)


if __name__=='__main__':unittest.main()
