import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path
from PIL import Image, ImageChops

SCRIPT = Path(__file__).resolve().with_name('photo_patch.py')
if SCRIPT.exists():
    spec=importlib.util.spec_from_file_location('photo_patch',SCRIPT)
    m=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
else:
    m=types.SimpleNamespace()

class PhotoPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.original=self.root/'source.png'
        image=Image.new('RGBA',(60,50))
        image.putdata([(x*3,y*4,x+y,255) for y in range(50) for x in range(60)])
        image.save(self.original)

    def prepare(self):
        self.assertTrue(callable(getattr(m,'prepare',None)),'prepare helper is missing')
        return m.prepare(self.original,(20,15,12,10),'把大改成小',self.root/'task')

    def test_prepare_records_normalized_photo_hash_and_native_context(self):
        task=self.prepare()
        manifest=json.loads(Path(task).read_text(encoding='utf-8'))
        self.assertEqual(manifest['rect'],[20,15,12,10])
        self.assertEqual(m.load_image(Path(task).parent/'reference.png').size,(60,50))
        self.assertEqual(len(manifest['original_sha256']),64)
        with self.assertRaises(ValueError):
            m.prepare(self.original,(-1,0,10,10),'x',self.root/'bad')

    def test_compose_resizes_full_photo_and_preserves_protected_pixels(self):
        task=self.prepare()
        candidate=self.root/'candidate.png'
        Image.new('RGB',(120,100),'red').save(candidate)
        protected=self.root/'protect.png'
        p=Image.new('L',(60,50));p.paste(255,(24,15,28,25));p.save(protected)
        out=self.root/'result.png'
        report=m.compose(task,candidate,'photo',out,protect_mask=protected,feather=2)
        self.assertEqual(report['changed_outside_allowed'],0)
        self.assertGreater(report['changed_inside_allowed'],0)
        a=Image.open(self.original).convert('RGBA');b=Image.open(out)
        self.assertEqual(a.getpixel((25,18)),b.getpixel((25,18)))
        self.assertEqual(a.getpixel((0,0)),b.getpixel((0,0)))
        self.assertEqual(b.size,(60,50))

    def test_reference_and_patch_frames_produce_same_local_result(self):
        task=self.prepare()
        candidate=self.root/'reference.png';Image.new('RGB',(60,50),'blue').save(candidate)
        patch=self.root/'patch.png';Image.new('RGB',(24,20),'blue').save(patch)
        a=self.root/'a.png';b=self.root/'b.png'
        m.compose(task,candidate,'reference',a)
        m.compose(task,patch,'patch',b)
        self.assertIsNone(ImageChops.difference(Image.open(a).convert('RGB'),Image.open(b).convert('RGB')).getbbox())

    def test_transparent_candidate_does_not_erase_photo(self):
        task=self.prepare();candidate=self.root/'transparent.png'
        Image.new('RGBA',(12,10),(0,0,0,0)).save(candidate)
        out=self.root/'same.png';report=m.compose(task,candidate,'patch',out)
        self.assertEqual(report['changed_inside_allowed'],0)

    def test_unknown_framing_and_modified_task_base_are_rejected(self):
        task=self.prepare();candidate=self.root/'candidate.png';Image.new('RGB',(100,100),'red').save(candidate)
        with self.assertRaisesRegex(ValueError,'比例|四角'):
            m.compose(task,candidate,'reference',self.root/'bad.png')
        base=Path(task).parent/'original.png';Image.new('RGB',(60,50),'white').save(base)
        with self.assertRaisesRegex(ValueError,'原图|hash|校验'):
            m.compose(task,candidate,'photo',self.root/'tampered.png')

    def test_verify_catches_external_change_and_rejects_crossed_quad(self):
        task=self.prepare()
        changed=self.root/'changed.png';image=Image.open(self.original);image.putpixel((0,0),(255,255,255,255));image.save(changed)
        report=m.verify(self.original,changed,Path(task).parent/'allowed-mask.png')
        self.assertEqual(report['changed_outside_allowed'],1)
        candidate=self.root/'candidate.png';Image.new('RGB',(60,50),'red').save(candidate)
        with self.assertRaisesRegex(ValueError,'四角'):
            m.compose(task,candidate,'photo',self.root/'crossed.png',quad=[[0,0],[60,50],[60,0],[0,50]])

    def test_feather_fully_replaces_interior_and_never_leaks_past_mask(self):
        self.prepare()
        mask=Image.new('L',(30,30),255)
        weights=m.inward_feather(mask,5)
        self.assertEqual(weights.getpixel((15,15)),255)
        self.assertLess(weights.getpixel((0,0)),255)
        mask.putpixel((15,15),0)
        self.assertEqual(m.inward_feather(mask,3).getpixel((15,15)),0)

    def test_identity_candidate_preserves_selected_gradient_exactly(self):
        task=self.prepare();out=self.root/'identity.png'
        report=m.compose(task,self.original,'photo',out)
        self.assertEqual(report['changed_inside_allowed'],0)

    def test_cropped_candidate_uses_exterior_frame_corners(self):
        task=self.prepare();out=self.root/'crop-result.png'
        candidate=self.root/'cropped.png'
        m.load_image(self.original).crop((10,10,50,40)).save(candidate)
        quad=[[-10,-10],[50,-10],[50,40],[-10,40]]
        report=m.compose(task,candidate,'reference',out,quad=quad)
        self.assertEqual(report['changed_inside_allowed'],0)
        self.assertEqual(report['changed_outside_allowed'],0)

    def test_exif_orientation_sets_the_coordinate_space(self):
        self.prepare()
        rotated=self.root/'rotated.jpg'
        exif=Image.Exif();exif[274]=6
        m.load_image(self.original).convert('RGB').save(rotated,exif=exif)
        task=m.prepare(rotated,(10,15,12,10),'改一个字',self.root/'rotated-task')
        manifest=json.loads(Path(task).read_text(encoding='utf-8'))
        self.assertEqual(manifest['size'],[50,60])

if __name__=='__main__':
    unittest.main()
