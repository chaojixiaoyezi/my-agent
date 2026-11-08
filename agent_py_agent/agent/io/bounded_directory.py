"""跨拍保留目录流，每次只消费一个条目；资格与权限仍由调用方核验。"""
import os


class DirectoryCursor:
    """只保存目录身份与迭代器，目录消失、换代、穷尽或读取异常时关闭。"""

    def __init__(self):
        self.entries = self.identity = None

    def close(self):
        entries, self.entries = self.entries, None
        self.identity = None
        if entries is not None:
            entries.close()

    def next_entry(self, directory):
        try:
            info = directory.stat()
            identity = info.st_dev, info.st_ino
            if identity != self.identity:
                self.close()
                self.entries = os.scandir(directory)
                self.identity = identity
            return next(self.entries)
        except (FileNotFoundError, NotADirectoryError, StopIteration):
            self.close()
            return None
        except BaseException:
            self.close()
            raise

    __del__ = close
