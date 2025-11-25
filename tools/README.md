## 修改zstack kvm初始化配置


---

**zstack_version:5.3.0**

---

**其他版本不确定，不要乱用**

- 修改目的 file 类型的设备 的初始化属性
    - **删除** 'snapshot=external'
    - 保证有 ‘discard=unmap’
    - 磁盘类型修改**virtio** 为 **scsi**
修改的目的文件
```
/var/lib/zstack/virtualenv/kvm/lib/python2.7/site-packages/kvmagent/plugins/vm_plugin.py
```

```
cp file_voleume_scsi.path  /var/lib/zstack/virtualenv/kvm/lib/python2.7/site-packages/kvmagent/plugins/
ce /var/lib/zstack/virtualenv/kvm/lib/python2.7/site-packages/kvmagent/plugins/
patch -p1 < file_voleume_scsi.path 
```


## 主机自带的qemu-img 不一定支持vdi格式。

当前版本仅限支持 **x86_64**
```

cd tools
bash lack.sh
```