"""Production follows the own upstream channel; writes require manual entry."""
import ast
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from watchdogs import config, game_updates, c5_firmware
from watchdogs.esp32_flash import FlashError
from test_flash_preflight import FlashEnvironment, ROOT, TREE


class FirmwarePolicyTests(unittest.TestCase):
    def environment(self):
        env=FlashEnvironment(allow_upstream=False);self.addCleanup(env.close)
        g=env.game;del g._check_fw_update
        g._flash_running=False;g._flash_sel=0;g.menu_open=True
        g._fw_remote_version='';g._fw_update_available=False;g._fw_package=None;g._fw_error=''
        return env

    def assert_board_untouched(self,env,*,threads=0,version='1.6.5'):
        self.assertEqual(env.events,[('thread-start',)]*threads)
        self.assertEqual(env.probe_calls,[]);self.assertEqual(env.flash_commands,[])
        self.assertEqual(len(env.threads),threads)
        self.assertTrue(env.game.serial.is_open);self.assertTrue(env.game._esp32)
        self.assertTrue(env.game.state.connected);self.assertTrue(env.usb_on)
        self.assertEqual(env.game._fw_version,version)
        self.assertFalse((env.root/'firmware_cache').exists())

    def test_production_uses_one_own_source_without_environment_override(self):
        self.assertIs(config.UPSTREAM_FIRMWARE_ENABLED,False)
        self.assertEqual(config.FIRMWARE_RELEASE_URL,'')  # [bristle] no online firmware source
        tree=ast.parse((ROOT/'watchdogs/config.py').read_text(encoding='utf-8'))
        definitions=[n for n in tree.body if isinstance(n,ast.Assign) and any(
            isinstance(t,ast.Name) and t.id=='FIRMWARE_RELEASE_URL' for t in n.targets)]
        self.assertEqual(len(definitions),1)
        environment=dict(os.environ,UPSTREAM_FIRMWARE_ENABLED='1',FIRMWARE_RELEASE_URL='https://example.invalid/unapproved')
        result=subprocess.run([sys.executable,'-B','-c',
            'from watchdogs import config,c5_firmware; assert config.UPSTREAM_FIRMWARE_ENABLED is False; '
            'assert config.FIRMWARE_RELEASE_URL==""'],cwd=ROOT,env=environment,
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_menu_preserves_existing_c5_firmware_action(self):
        rows=[n for n in ast.walk(TREE) if isinstance(n,ast.Tuple) and len(n.elts)==5
            and isinstance(n.elts[3],ast.Constant) and n.elts[3].value=='_flash_esp']
        self.assertEqual(len(rows),1)
        self.assertEqual(ast.literal_eval(rows[0].elts[1]),'Firmware C5/SOLO')
        self.assertEqual(ast.literal_eval(rows[0].elts[2]),'_flash_esp32')

    def test_default_checker_reads_only_c5_metadata_and_announces_c5_version(self):
        env=self.environment();env.game._check_fw_update()
        self.assertEqual(env.c5_calls,[('metadata',)])
        self.assertEqual(env.game._fw_remote_version,'1.7.0');self.assertTrue(env.game._fw_update_available)
        self.assertIs(env.game._fw_package,env.c5_package)
        self.assertIn('C5',env.messages[0][0]);self.assert_board_untouched(env)

    def test_missing_c5_package_clears_stock_notice_without_fallback(self):
        env=self.environment();g=env.game;g._fw_remote_version='1.7.3';g._fw_update_available=True
        env.c5_fetch_error=c5_firmware.C5FirmwareError('No compatible C5/SOLO package')
        g._check_fw_update()
        self.assertEqual(env.c5_calls,[('metadata',)]);self.assertFalse(g._fw_update_available)
        self.assertEqual(g._fw_remote_version,'');self.assertIn('No compatible',g._fw_error)
        self.assertEqual(env.messages,[]);self.assert_board_untouched(env)

    def test_equal_reported_version_is_not_new_or_a_hash_certificate(self):
        env=self.environment();env.game._fw_version='1.7.0';env.game._check_fw_update()
        self.assertFalse(env.game._fw_update_available);self.assertEqual(env.game._fw_remote_version,'1.7.0')
        self.assertEqual(env.messages,[]);self.assert_board_untouched(env,version='1.7.0')

    def test_serial_version_starts_one_metadata_check_without_installing(self):
        env=self.environment();g=env.game;g._fw_version='';g.loot=None;handled=[]
        g._solo=SimpleNamespace(confirmed_wifi=False,confirmed_ble=False,
            handle_line=lambda line:handled.append(line) or True)
        g._handle_serial_line('JanOS version: 1.7.0');g._handle_serial_line('JanOS version: 1.7.0')
        self.assertEqual(handled,['JanOS version: 1.7.0']*2)
        self.assertEqual(env.threads[0].target.__name__,'_check_fw_update')
        self.assertTrue(env.threads[0].daemon);self.assertEqual(env.c5_calls,[])
        self.assert_board_untouched(env,threads=1,version='1.7.0')

    def test_open_and_refresh_schedule_read_only_checks_enter_waits_for_them(self):
        env=self.environment();g=env.game;g._start_flash_esp32()
        self.assertTrue(g._flash_screen);self.assertFalse(g.menu_open);self.assertEqual(g._flash_boards,['xiao'])
        self.assertTrue(g._c5_probe_requested)
        self.assertTrue(g._c5_dependency_running)
        self.assertEqual([t.target.__name__ for t in env.threads],['_c5_check_python','_check_fw_update'])
        self.assertEqual(env.threads[0].kwargs,{'repair':False})
        self.assertTrue(all(t.daemon for t in env.threads))
        for key in (env.px.KEY_DOWN,env.px.KEY_UP,env.px.KEY_RETURN):
            env.buttons={key};g._update_flash_screen()
        self.assertFalse(g._flash_running);self.assertEqual(g._flash_sel,0)
        # A scheduled check is not permission to install, repair or touch USB.
        self.assert_board_untouched(env,threads=2)
        g._c5_dependency_running=False;g._fw_checking=True
        env.buttons={env.px.KEY_R};g._update_flash_screen()
        self.assertEqual(len(env.threads),2)
        g._fw_checking=False;g._update_flash_screen()
        self.assertEqual([t.target.__name__ for t in env.threads],
                         ['_c5_check_python','_check_fw_update','_check_fw_update','_c5_check_python'])
        self.assertEqual(env.threads[-1].kwargs,{'repair':False})
        self.assert_board_untouched(env,threads=4)

    def test_explicit_p_schedules_only_dependency_repair_without_installing_firmware(self):
        env=self.environment();g=env.game
        env.buttons={env.px.KEY_P};g._update_flash_screen()
        self.assertEqual(len(env.threads),1)
        self.assertEqual(env.threads[0].target.__name__,'_c5_check_python')
        self.assertEqual(env.threads[0].kwargs,{'repair':True})
        self.assertTrue(env.threads[0].daemon)
        self.assert_board_untouched(env,threads=1)

    def test_busy_worker_cannot_release_existing_operation_guard(self):
        env=self.environment();g=env.game;g._flash_running=True;g._flash_job_lock.acquire()
        try:
            g._run_flash('xiao');self.assertTrue(g._flash_job_lock.locked());self.assertTrue(g._flash_running)
        finally:g._flash_job_lock.release()
        self.assertEqual(env.c5_calls,[]);self.assert_board_untouched(env)

    def test_unsupported_board_refused_before_tool_cache_transport_download(self):
        env=self.environment()
        with self.assertRaises(FlashError):env.game._flash_do('wroom')
        self.assertEqual(env.c5_calls,[]);self.assert_board_untouched(env)

    def test_no_bundled_firmware_is_not_announced_as_a_failed_check(self):
        # [bristle] a build without the firmware folder says nothing at startup; the screen says it
        env=self.environment()
        env.c5_fetch_error=env.c5_helpers.C5FirmwareError('No bundled firmware in this build')
        env.game._check_fw_update()
        self.assertEqual(env.game._fw_error,'No bundled firmware in this build')
        self.assertFalse(any('[FW]' in text for text in env.output),env.output)
        env.c5_fetch_error=env.c5_helpers.C5FirmwareError('Synthetic network failure')
        env.game._check_fw_update()
        self.assertTrue(any('source check failed' in text for text in env.output))

    def test_renderer_says_so_when_the_build_ships_no_firmware(self):
        # [bristle] a build without the firmware folder: the screen says it and offers no package
        env=self.environment();env.c5_helpers.bundled_available=lambda *args:False
        env.game._fw_package=None;env.game._draw_flash_screen()
        words=' '.join(args[2] for name,args in env.draw_calls if name=='text')
        self.assertIn('No bundled firmware in this build',words)
        self.assertIn('Nothing will be flashed',words)
        self.assertNotIn('Approved C5 package',words)
        for name,args in env.draw_calls:
            if name=='text':self.assertLessEqual(len(args[2]),80,args[2])

    def test_renderer_shows_current_latest_source_target_and_manual_controls(self):
        env=self.environment();env.c5_helpers.bundled_available=lambda *args:True  # [bristle] this test: a build that ships it
        env.game._fw_package=env.c5_package;env.game._draw_flash_screen()
        words=' '.join(args[2] for name,args in env.draw_calls if name=='text')
        for text in ('C5/SOLO FIRMWARE UPDATE','Reported JanOS: 1.6.5',
                     'SOLO: unknown / checking; a version number is not proof',
                     'Approved C5 package: JanOS 1.7.0 + SOLO','Source: shipped with this build',
                     'XIAO ESP32-C5 rev 1.0','8 MiB','Application only','P repair Python',
                     'R check','M migrate','ENTER install','ESC close'):
            self.assertIn(text,words)
        for text in ('disabled','No automatic firmware channel','Select Board','1.7.3','hash verified','C5 1.6.5'):
            self.assertNotIn(text,words)
        for name,args in env.draw_calls:
            if name=='text':
                x,y,text,_=args;self.assertGreaterEqual(x,0);self.assertGreaterEqual(y,0)
                self.assertLessEqual(x+len(text)*5,640);self.assertLessEqual(y+8,360)
        self.assert_board_untouched(env)

    def test_escape_closes_window_without_starting_job(self):
        env=self.environment();g=env.game;g._fw_package=env.c5_package;g._start_flash_esp32()
        env.buttons={env.px.KEY_ESCAPE};g._update_flash_screen()
        self.assertFalse(g._flash_screen);self.assertEqual(g._esc_consumed_frame,env.px.frame_count)
        self.assertEqual([t.target.__name__ for t in env.threads],['_c5_check_python'])
        self.assertEqual(env.threads[0].kwargs,{'repair':False})
        self.assert_board_untouched(env,threads=1)

    def test_game_updater_and_separate_piglet_keep_existing_identity(self):
        self.assertEqual(game_updates.REPOSITORY_ID,0);self.assertEqual(game_updates.OWNER_ID,0);self.assertEqual(game_updates.CHANNEL,'stable')
        if not (ROOT/'firmware/piglet-collection/manifest.json').is_file():
            self.skipTest('No bundled firmware in this build')  # [bristle]
        piglet=json.loads((ROOT/'firmware/piglet-collection/manifest.json').read_text())
        self.assertEqual(piglet['target']['app_offset'],'0x10000')
        self.assertEqual(piglet['target']['board'],'Seeed XIAO ESP32-C5')
        self.assertRegex(piglet['provenance']['repository'],r'^[A-Za-z0-9-]+/Piglet-[A-Za-z0-9-]+$')  # exact bytes: SHA256SUMS


if __name__=='__main__':unittest.main()
