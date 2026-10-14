package app.obsidianvow.core;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.*;
import android.graphics.drawable.*;
import android.animation.ValueAnimator;
import android.view.View;

/** Resolution-independent details for the native companionship surface. */
final class TogetherChrome {
    static final int INK=0xFF131D29, TEXT=0xFFF4EBDD, MUTED=0xFFB2B9B9,
        GOLD=0xFFE1C18B, LINE=0xFF6A624F, SOFT=0xFF213A43, ROSE=0xFFE3B0BC;
    static int dp(Context context,float value){return Math.round(value*context.getResources().getDisplayMetrics().density);}
    static GradientDrawable shape(Context context,int color,float radius,int stroke){
        GradientDrawable drawable=new GradientDrawable();drawable.setColor(color);drawable.setCornerRadius(dp(context,radius));
        if(stroke!=0)drawable.setStroke(dp(context,1),stroke);return drawable;
    }
    static Drawable touch(Context context,int color,float radius,int stroke){
        return new RippleDrawable(ColorStateList.valueOf(0x22D0B181),shape(context,color,radius,stroke),shape(context,Color.WHITE,radius,0));
    }
    static Drawable card(Context context){
        return new Enamel(context);
    }
    static Drawable entryBackground(Context context){return new Entry(context);}
    private static final class Entry extends Drawable {
        private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);
        private final float density;
        Entry(Context context){density=context.getResources().getDisplayMetrics().density;}
        @Override public void draw(Canvas c){
            Rect b=getBounds();float w=b.width()/density,h=b.height()/density;
            c.save();c.translate(b.left,b.top);c.scale(density,density);
            paint.setStyle(Paint.Style.FILL);paint.setColor(Color.WHITE);paint.setShader(new LinearGradient(0,0,w,h,new int[]{0xFF172732,INK,0xFF191F2D},new float[]{0,.6f,1},Shader.TileMode.CLAMP));c.drawRect(0,0,w,h,paint);
            paint.setShader(new RadialGradient(w/2,136,w*.7f,new int[]{0x445C7974,Color.TRANSPARENT},null,Shader.TileMode.CLAMP));c.drawRect(0,0,w,h,paint);paint.setShader(null);
            paint.setStyle(Paint.Style.STROKE);paint.setStrokeWidth(.65f);paint.setColor(0x558E9C88);
            c.drawArc(w/2-66,70,w/2+66,202,202,136,false,paint);c.drawArc(w/2-72,64,w/2+72,208,23,134,false,paint);
            paint.setColor(0x77CBB789);c.drawLine(30,136,w/2-81,136,paint);c.drawLine(w/2+81,136,w-30,136,paint);diamond(c,paint,w/2,61,2.5f);diamond(c,paint,27,136,2.5f);diamond(c,paint,w-27,136,2.5f);
            paint.setColor(0x227A9B95);c.drawLine(12,210,12,h-25,paint);c.drawLine(w-12,210,w-12,h-25,paint);
            c.restore();
        }
        @Override public void setAlpha(int alpha){}
        @Override public void setColorFilter(ColorFilter filter){}
        @Override public int getOpacity(){return PixelFormat.OPAQUE;}
    }
    static Drawable message(Context context,boolean owner){
        GradientDrawable background=new GradientDrawable(GradientDrawable.Orientation.TL_BR,
            owner?new int[]{0xF24D364C,0xF2332B3B}:new int[]{0xF22B4850,0xF21D303D});
        float r=dp(context,13),small=dp(context,4);
        background.setCornerRadii(owner?new float[]{r,r,small,small,r,r,r,r}:new float[]{small,small,r,r,r,r,r,r});
        background.setStroke(dp(context,.65f),owner?0x887B6677:0x88718B88);return background;
    }
    /** Decoration is a background drawable; it never intercepts touches. */
    private static final class Enamel extends Drawable {
        private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);
        private final float density;
        Enamel(Context context){density=context.getResources().getDisplayMetrics().density;}
        @Override public void draw(Canvas canvas){
            Rect b=getBounds();float w=b.width()/density,h=b.height()/density;
            canvas.save();canvas.translate(b.left,b.top);canvas.scale(density,density);
            RectF edge=new RectF(.75f,.75f,w-.75f,h-.75f);Path clip=new Path();clip.addRoundRect(edge,22,22,Path.Direction.CW);canvas.clipPath(clip);
            paint.setStyle(Paint.Style.FILL);paint.setColor(Color.WHITE);paint.setShader(new LinearGradient(0,0,w,h,new int[]{0xFF293B46,0xFF182531,0xFF202635},new float[]{0,.48f,1},Shader.TileMode.CLAMP));canvas.drawRect(0,0,w,h,paint);
            paint.setShader(new RadialGradient(w*.88f,14,w*.7f,new int[]{0x55597E76,Color.TRANSPARENT},null,Shader.TileMode.CLAMP));canvas.drawRect(0,0,w,h,paint);paint.setShader(null);
            // Fine diagonal enamel grain, kept below the opaque message surfaces.
            paint.setColor(0x089FCCD0);paint.setStrokeWidth(.4f);
            for(float x=-h;x<w;x+=12)canvas.drawLine(x,0,x+h,h,paint);
            paint.setStyle(Paint.Style.STROKE);paint.setStrokeWidth(.8f);
            paint.setColor(Color.WHITE);paint.setShader(new LinearGradient(0,0,w,h,new int[]{0xFFEAD2A1,0xFF837352,0xFFD3B582},null,Shader.TileMode.CLAMP));canvas.drawRoundRect(edge,22,22,paint);paint.setShader(null);
            paint.setColor(0x667DABAB);canvas.drawRoundRect(4,4,w-4,h-4,18,18,paint);
            paint.setColor(0x99D3B582);paint.setStrokeWidth(.65f);
            for(int corner=0;corner<4;corner++){
                canvas.save();canvas.translate(corner%2==0?8:w-8,corner<2?8:h-8);canvas.scale(corner%2==0?1:-1,corner<2?1:-1);flourish(canvas,paint);canvas.restore();
            }
            // A small inlaid crest sits in the quiet strip between header and chat.
            float mid=w/2,cy=57;paint.setColor(0x66DDC08A);paint.setStrokeWidth(.65f);
            canvas.drawLine(44,cy,mid-12,cy,paint);canvas.drawLine(mid+12,cy,w-44,cy,paint);
            diamond(canvas,paint,mid,cy,3.5f);canvas.drawCircle(mid-8,cy,1,paint);canvas.drawCircle(mid+8,cy,1,paint);
            canvas.restore();
        }
        private void flourish(Canvas c,Paint p){
            Path stem=new Path();stem.moveTo(0,35);stem.cubicTo(0,11,6,0,35,0);c.drawPath(stem,p);
            Path scroll=new Path();scroll.moveTo(3,24);scroll.cubicTo(8,13,26,20,20,8);scroll.cubicTo(17,2,10,6,14,10);c.drawPath(scroll,p);
            for(int i=0;i<3;i++){
                float x=8+i*7,y=3+(2-i)*2;Path leaf=new Path();leaf.moveTo(x,y);leaf.quadTo(x+1,y+7,x+6,y+7);leaf.quadTo(x+8,y+1,x,y);c.drawPath(leaf,p);
            }
            diamond(c,p,2,38,1.7f);diamond(c,p,38,2,1.7f);
        }
        @Override public void getOutline(Outline outline){outline.setRoundRect(getBounds(),22*density);}
        @Override public void setAlpha(int alpha){}
        @Override public void setColorFilter(ColorFilter filter){}
        @Override public int getOpacity(){return PixelFormat.TRANSLUCENT;}
    }
    private static void diamond(Canvas c,Paint paint,float x,float y,float r){
        Path path=new Path();path.moveTo(x,y-r);path.lineTo(x+r,y);path.lineTo(x,y+r);path.lineTo(x-r,y);path.close();c.drawPath(path,paint);
    }
    static final class Icon extends Drawable {
        private final String kind;
        private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);
        Icon(String kind,int color){this.kind=kind;paint.setColor(color);paint.setStrokeWidth(1.65f);paint.setStyle(Paint.Style.STROKE);paint.setStrokeCap(Paint.Cap.ROUND);paint.setStrokeJoin(Paint.Join.ROUND);}
        private void line(Canvas c,float x,float y,float xx,float yy){c.drawLine(x,y,xx,yy,paint);}
        private void path(Canvas c,float... points){Path p=new Path();p.moveTo(points[0],points[1]);for(int i=2;i<points.length;i+=2)p.lineTo(points[i],points[i+1]);c.drawPath(p,paint);}
        @Override public void draw(Canvas c){
            Rect b=getBounds();c.save();c.translate(b.left,b.top);c.scale(b.width()/24f,b.height()/24f);
            switch(kind){
                case "chat":c.drawRoundRect(3,4,21,18,4,4,paint);path(c,8,18,6,22,13,18);line(c,7,9,17,9);line(c,7,13,14,13);break;
                case "collapse":line(c,6,12,18,12);break;
                case "more":paint.setStyle(Paint.Style.FILL);for(int x=5;x<=19;x+=7)c.drawCircle(x,12,1.45f,paint);paint.setStyle(Paint.Style.STROKE);break;
                case "close":line(c,7,7,17,17);line(c,17,7,7,17);break;
                case "send":path(c,6,11,12,5,18,11);line(c,12,5,12,19);break;
                case "mic":c.drawRoundRect(9,3,15,14,3,3,paint);c.drawArc(6,6,18,18,0,180,false,paint);line(c,12,18,12,21);line(c,9,21,15,21);break;
                case "stop":c.drawRoundRect(6,6,18,18,2,2,paint);break;
                case "camera":path(c,4,7,7,7,9,4,15,4,17,7,20,7,21,8,21,18,20,19,4,19,3,18,3,8,4,7);c.drawCircle(12,12.5f,3.3f,paint);break;
                case "screen":c.drawRoundRect(3,4,21,17,2,2,paint);line(c,12,17,12,21);line(c,8,21,16,21);break;
                case "pause":line(c,9,5,9,19);line(c,15,5,15,19);break;
                case "play":path(c,8,5,19,12,8,19,8,5);break;
                case "end":c.drawArc(4,4,20,20,-45,270,false,paint);line(c,12,2,12,11);break;
                case "retry":c.drawArc(5,5,19,19,-45,290,false,paint);path(c,19,4,19,9,14,9);break;
                case "switch":path(c,4,8,19,8,15,4);path(c,20,16,5,16,9,20);break;
                case "image":c.drawRoundRect(3,3,21,21,3,3,paint);c.drawCircle(8,8,1.4f,paint);path(c,4,17,10,11,14,15,17,12,21,16);break;
            }
            c.restore();
        }
        @Override public void setAlpha(int alpha){paint.setAlpha(alpha);invalidateSelf();}
        @Override public void setColorFilter(ColorFilter filter){paint.setColorFilter(filter);invalidateSelf();}
        @Override public int getOpacity(){return PixelFormat.TRANSLUCENT;}
    }
    static final class Seal extends View {
        private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);
        private Bitmap avatar;
        private BitmapShader portrait;
        private ValueAnimator pulse;
        private float glow;
        private boolean unread;
        Seal(Context context){super(context);setLayerType(View.LAYER_TYPE_SOFTWARE,null);setContentDescription("展开聊天浮窗");}
        void avatar(Bitmap bitmap){avatar=bitmap;portrait=bitmap==null?null:new BitmapShader(bitmap,Shader.TileMode.CLAMP,Shader.TileMode.CLAMP);invalidate();}
        void name(String name){}
        void unread(boolean value){if(unread!=value){unread=value;invalidate();}}
        void pulse(){
            stopPulse();pulse=ValueAnimator.ofFloat(0,1);pulse.setDuration(1100);pulse.setRepeatCount(1);
            pulse.addUpdateListener(animation->{glow=(float)Math.sin((float)animation.getAnimatedValue()*Math.PI);invalidate();});pulse.start();
        }
        void stopPulse(){if(pulse!=null){pulse.cancel();pulse=null;}glow=0;invalidate();}
        @Override protected void onDetachedFromWindow(){stopPulse();super.onDetachedFromWindow();}
        @Override protected void onDraw(Canvas c){
            float size=Math.min(getWidth(),getHeight()),cx=getWidth()/2f,cy=getHeight()/2f,r=size*.425f;
            paint.setStyle(Paint.Style.FILL);paint.setShader(null);paint.setColor(INK);paint.setShadowLayer(size*(.05f+glow*.055f),0,size*.02f,glow>0?0xCCF2D19A:0x77000000);c.drawCircle(cx,cy,r,paint);paint.clearShadowLayer();
            if(portrait!=null&&avatar!=null&&!avatar.isRecycled()){
                float radius=r-size*.065f,scale=radius*2/Math.min(avatar.getWidth(),avatar.getHeight());Matrix matrix=new Matrix();matrix.setScale(scale,scale);matrix.postTranslate(cx-avatar.getWidth()*scale/2,cy-avatar.getHeight()*scale/2);portrait.setLocalMatrix(matrix);
                paint.setShader(portrait);c.drawCircle(cx,cy,radius,paint);paint.setShader(null);
            }else{paint.setColor(GOLD);diamond(c,paint,cx,cy,size*.16f);}
            paint.setStyle(Paint.Style.STROKE);paint.setStrokeWidth(Math.max(1,size*.014f));
            paint.setShader(new LinearGradient(cx-r,cy-r,cx+r,cy+r,new int[]{0xFFFAE6B8,0xFF977447,0xFFE8CF98},null,Shader.TileMode.CLAMP));c.drawCircle(cx,cy,r,paint);c.drawCircle(cx,cy,r-size*.043f,paint);paint.setShader(null);
            for(int i=0;i<12;i++){double a=i*Math.PI/6;float x=cx+(float)Math.cos(a)*(r-size*.02f),y=cy+(float)Math.sin(a)*(r-size*.02f);paint.setColor(GOLD);c.drawCircle(x,y,size*.009f,paint);}
            paint.setStyle(Paint.Style.FILL);paint.setColor(GOLD);diamond(c,paint,cx,cy-r,size*.044f);diamond(c,paint,cx,cy+r,size*.035f);
            if(unread){float x=cx+r*.73f,y=cy-r*.72f;paint.setColor(INK);c.drawCircle(x,y,size*.105f,paint);paint.setColor(GOLD);c.drawCircle(x,y,size*.072f,paint);}
        }
    }
}
