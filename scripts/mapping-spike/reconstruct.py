"""Reconstruct one stretch with COLMAP (through pycolmap) and report what came out."""

import sys
import shutil
import time
from pathlib import Path
import numpy as np
import pycolmap

stretch = Path(sys.argv[1])
images, masks, work = stretch / "images", stretch / "masks", stretch / "work"
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True)
db = work / "database.db"
t0 = time.time()
reader = pycolmap.ImageReaderOptions()
reader.mask_path = str(masks)
reader.camera_model = "SIMPLE_RADIAL"
focal = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
reader.camera_params = f"{focal},480,300,0"  # a starting lens; refined afterwards
pycolmap.extract_features(db, images, camera_mode=pycolmap.CameraMode.SINGLE, reader_options=reader)
pycolmap.match_exhaustive(db)
opts = pycolmap.IncrementalPipelineOptions()
# The aircraft flies at what it looks at. COLMAP's defaults reject that as a
# starting pair and ask for more sideways shift than forward flight gives.
opts.mapper.init_max_forward_motion = 1.0
opts.mapper.init_min_tri_angle = 1.5
opts.mapper.init_min_num_inliers = 50
opts.mapper.abs_pose_min_num_inliers = 20
opts.mapper.filter_min_tri_angle = 0.5
opts.triangulation.min_angle = 0.5
opts.min_model_size = 5
recs = pycolmap.incremental_mapping(db, images, work / "sparse", options=opts)
print("starting focal", focal)
n_images = len(list(images.glob("*.jpg")))
print("images", n_images, "| models", len(recs), "| time %.0f s" % (time.time() - t0))
for idx, rec in recs.items():
    errs = [p.error for p in rec.points3D.values()]
    cam = next(iter(rec.cameras.values()))
    print(
        f"model {idx}: registered {rec.num_reg_images()} of {n_images} images, {rec.num_points3D()} points, "
        f"mean reprojection error {np.mean(errs):.2f} px, mean track length {rec.compute_mean_track_length():.1f}, "
        f"focal {cam.params[0]:.0f} px for a {cam.width} px wide image"
    )
    centres = np.array(
        [
            im.projection_center()
            for im in sorted(rec.images.values(), key=lambda im: im.name)
            if im.has_pose
        ]
    )
    steps = np.linalg.norm(np.diff(centres, axis=0), axis=1)
    pts = np.array([p.xyz for p in rec.points3D.values()])
    print(
        "   camera steps between frames (model units): median %.3f, min %.3f, max %.3f"
        % (np.median(steps), steps.min(), steps.max())
    )
    print("   point cloud extent (model units):", np.round(pts.max(0) - pts.min(0), 1))
    np.save(work / f"centres_{idx}.npy", centres)
    np.save(work / f"points_{idx}.npy", pts)
    np.save(work / f"colors_{idx}.npy", np.array([p.color for p in rec.points3D.values()]))
