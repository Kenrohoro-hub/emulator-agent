from collections import OrderedDict
from io import BytesIO
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from emulator_agent.controller import Controller, Slot, integer
from emulator_agent.discovery import ControlError, Instance, parse_ld, parse_mumu
from emulator_agent.workers import MAX_RESPONSE_BYTES, Worker


class CoreTests(unittest.TestCase):
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

    def controller_stub(self):
        c=Controller.__new__(Controller)
        c.slots={};c.map_lock=threading.Lock()
        c.log=Mock()
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
