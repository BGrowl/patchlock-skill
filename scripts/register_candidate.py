"""Optional unchanged-background registration. Requires OpenCV and NumPy locally."""
import argparse
import json
from pathlib import Path
from photo_patch import read_task, load_image, frame_rect, quad_transform, new_output


def register(task_path, candidate_path, frame, output):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise ValueError('自动配准需要本地 OpenCV 与 NumPy；也可提供人工四角映射，不需要 API Key') from exc
    task, original = read_task(task_path)
    fx, fy, fw, fh = frame_rect(task, frame)
    reference = original.crop((fx,fy,fx+fw,fy+fh)).convert('RGB')
    candidate = load_image(candidate_path).convert('RGB')

    def thumbnail(image):
        w, h = image.size
        scale = min(1,1600/max(w,h))
        size = max(1,round(w*scale)), max(1,round(h*scale))
        small = image.resize(size)
        return cv2.cvtColor(np.array(small),cv2.COLOR_RGB2GRAY), np.array([size[0]/w,size[1]/h])

    a, sa = thumbnail(reference)
    b, sb = thumbnail(candidate)
    # Feature correspondences come only from unchanged reference background.
    x,y,w,h = task['rect']
    mask = np.full(a.shape,255,dtype=np.uint8)
    left,top = np.floor((np.array([x-fx,y-fy])-4)*sa).astype(int)
    right,bottom = np.ceil((np.array([x+w-fx,y+h-fy])+4)*sa).astype(int)
    mask[max(0,top):max(0,bottom),max(0,left):max(0,right)] = 0
    sift = cv2.SIFT_create(nfeatures=6000)
    ka,da = sift.detectAndCompute(a,mask)
    kb,db = sift.detectAndCompute(b,None)
    if da is None or db is None or len(da)<12 or len(db)<12:
        raise ValueError('稳定背景特征不足，请提供完整参考图或人工对应点')
    pairs = cv2.BFMatcher().knnMatch(da,db,k=2)
    good = [pair[0] for pair in pairs if len(pair)==2 and pair[0].distance<.7*pair[1].distance]
    if len(good)<12:
        raise ValueError('可靠背景匹配不足，不能确定裁切或透视关系')
    src = np.float64([ka[m.queryIdx].pt for m in good])/sa
    dst = np.float64([kb[m.trainIdx].pt for m in good])/sb
    matrix,inliers = cv2.findHomography(src,dst,cv2.RANSAC,3/float(min(sb)))
    if matrix is None or inliers is None:
        raise ValueError('未找到可靠透视映射，请重新生成完整参考图')
    inside = inliers.ravel().astype(bool)
    if int(inside.sum())<12 or float(inside.mean())<.4:
        raise ValueError('背景匹配一致性不足，请人工检查定位点')
    inverse = np.linalg.inv(matrix)
    projected = cv2.perspectiveTransform(dst[:,None,:],inverse)[:,0,:]
    errors = np.linalg.norm(projected-src,axis=1)
    center = np.array([x-fx+w/2,y-fy+h/2])
    near = inside & (np.linalg.norm(src-center,axis=1)<max(180,3*w,3*h))
    if int(near.sum())<6 or float(np.median(errors[near]))>2.5:
        raise ValueError('修改区附近的稳定匹配不足或误差过大，需人工对应点')
    frame_corners = np.float64([[0,0],[fw,0],[fw,fh],[0,fh]])
    quad = cv2.perspectiveTransform(frame_corners[:,None,:],matrix)[:,0,:].tolist()
    quad_transform(quad)
    local_corners=np.float64([[x-fx,y-fy],[x-fx+w,y-fy],[x-fx+w,y-fy+h],[x-fx,y-fy+h]])
    mapped=cv2.perspectiveTransform(local_corners[:,None,:],matrix)[:,0,:]
    if not np.isfinite(mapped).all() or (mapped<-.01).any() or (mapped[:,0]>candidate.width+.01).any() or (mapped[:,1]>candidate.height+.01).any():
        raise ValueError('候选图缺失修改区域，请提供覆盖完整修改区的图片')
    output=new_output(output)
    stats_output=new_output(output.with_name(output.stem+'.alignment.json'))
    report=dict(frame=frame,quad=quad,matches=len(good),inliers=int(inside.sum()),
                nearby_inliers=int(near.sum()),nearby_median_error_reference_px=float(np.median(errors[near])),
                limits='背景几何拟合指标不证明字形正确或局部没有变形，需要检查。')
    output.write_text(json.dumps(quad,indent=2),encoding='utf-8')
    stats_output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task',required=True)
    parser.add_argument('--candidate',required=True)
    parser.add_argument('--frame',choices=['reference','photo'],required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(register(args.task,args.candidate,args.frame,args.output),ensure_ascii=False))
    except (ValueError,OSError,KeyError) as exc:
        parser.exit(2,f'Error: {exc}\n')
