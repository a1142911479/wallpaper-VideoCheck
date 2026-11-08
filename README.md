# wallpaper-VideoCheck
AI使用python，解决steam的wallpaper，下载了重复的视频，程序会找到重复的视频，自己决定是否删除
# 使用方式CLI：
python3 main.py --video-dir /mnt/ssd/videos --multithread --workers 8 --md5-chunk-size-mb 4
# 使用方式gui：
python3 gui.py

# TBD
1.需要一个程序，设定取wallpaper的目录，找到wallpaper的配置json，通过这个配置json，把上一个程序找到的重复视频删除并取消订阅
2.可选功能是，修改配置json，对我已有的视频进行分类。
        分类方案1：通过时长分类，
        分类方案2：通过视频作者或者视频画风分类视频的方案(stash+stashDB+thePornDB)
