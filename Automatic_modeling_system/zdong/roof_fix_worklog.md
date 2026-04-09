# 屋顶修复工作日志

**日期**: 2026-04-09
**修复文件**: `app/pipeline.py`
**修复范围**: `ExportService._add_gable_slope_shape()` 方法

---

## 问题描述

通过建模意图描述生成的模型中，双坡屋顶(gable roof)出现三个问题：

1. **屋顶呈现片状**: 双坡屋顶没有正确的 3D 几何形状，看起来像扁平的薄片
2. **屋顶无法覆盖建筑**: 屋顶尺寸和位置不正确，无法完全覆盖下方建筑
3. **两坡面之间存在缝隙**: 左右两块坡面在屋脊处不闭合，存在可见缝隙

## 根因分析

### 问题 1 & 2：拉伸方向错误

在 `_add_gable_slope_shape()` 方法中，IFC ExtrudedAreaSolid 的拉伸方向设置错误。

#### 错误代码

```python
# 原始代码（有 bug）
extrusion_dir = writer.add("IFCDIRECTION((1.,0,0.))")  # 同时用作 Axis 和 ExtrudedDirection
x_ref = writer.add("IFCDIRECTION((0.,1.,0.))")
solid_axis = writer.add(f"IFCAXIS2PLACEMENT3D(#{solid_origin},#{extrusion_dir},#{x_ref})")
solid = writer.add(f"IFCEXTRUDEDAREASOLID(#{profile},#{solid_axis},#{extrusion_dir},...)")
```

#### 原因详解

在 IFC 规范中，`IfcExtrudedAreaSolid` 的 `ExtrudedDirection` 是在 **Position 自身坐标系** 中解释的，而不是在父坐标系中。

Position 坐标系到父坐标系的映射为：
| Position 轴 | 方向 | 对应建筑轴 |
|---|---|---|
| X (RefDirection) | `(0,1,0)` | Y (深度) |
| Y | `(0,0,1)` | Z (高度) |
| Z (Axis) | `(1,0,0)` | X (宽度) |

当 `ExtrudedDirection = (1,0,0)` 时，在 Position 坐标系中这是 X 轴方向，映射到父坐标系为 `(0,1,0)`，即**建筑 Y 轴（深度方向）**，而非建筑 X 轴（宽度方向）。

结果：屋顶沿 Y 轴拉伸 `building_width`，截面在 YZ 平面仅 0.15m 厚，呈现为薄片状且无法覆盖建筑。

### 问题 3：两坡面屋脊缝隙

坡面截面使用等厚度(0.15m)偏移，厚度方向垂直于坡面法线。在屋脊处（x=0），两个坡面的顶部顶点分别为：

- 左坡面 p4 = `(dx, ridge_height + dy)` ≈ `(0.07, 4.17)`
- 右坡面 p4 = `(-dx, ridge_height + dy)` ≈ `(-0.07, 4.17)`

缝隙宽度 = `2 * dx ≈ 0.14m`（约 14cm），在视觉上清晰可见。

#### 缝隙产生原因

```
                缝隙 (0.14m)
           ┌───┐     ┌───┐
          /│   │     │   │\
         / │   │     │   │ \
        /  │   │     │   │  \
       /   │   │     │   │   \
      /    │   │     │   │    \
     /     │ 左│     │右 │     \
    ───────┘   └─────┘   └──────
```

法线方向的厚度偏移在屋脊处将两个坡面的外表面向外推开，形成 V 形缺口。

## 修复方案

### 修复 1：拉伸方向（问题 1 & 2）

将 `ExtrudedDirection` 从 `(1,0,0)` 改为 `(0,0,1)`，并分离 Axis 和 ExtrudedDirection 为独立实体。

```python
# 修复后
axis_dir = writer.add("IFCDIRECTION((1.,0.,0.))")       # Position Z = 建筑 X 轴
extrusion_dir = writer.add("IFCDIRECTION((0.,0.,1.))")   # 沿 Position Z 拉伸 = 沿建筑 X 拉伸
x_ref = writer.add("IFCDIRECTION((0.,1.,0.))")           # Position X = 建筑 Y 轴

solid_axis = writer.add(f"IFCAXIS2PLACEMENT3D(#{solid_origin},#{axis_dir},#{x_ref})")
solid = writer.add(f"IFCEXTRUDEDAREASOLID(#{profile},#{solid_axis},#{extrusion_dir},...)")
```

### 修复 2：屋脊缝隙（问题 3）

将两个坡面的屋脊顶部顶点 (p4) 设为 `(0, ridge_height)`，使外表面在屋脊中心线汇合。

厚度从檐口处的满厚 (0.15m) 向屋脊处逐渐收窄至零，这是双坡屋顶截面的标准做法。

```python
# 修复后：左坡面
p4 = (0., ridge_height)          # 屋脊顶点在中心线

# 修复后：右坡面
p4 = (0., ridge_height)          # 同样在中心线汇合
```

```
修复后：两坡面在屋脊处无缝闭合
              ┌┐
             /||\
            / || \
           /  ||  \
          /   ||   \
         /    ||    \
        /     ||     \
       ───────┘└───────
```

## 验证结果

1. **Pipeline 测试通过**: 成功生成 text_only 模式的双坡屋顶模型
2. **IFC 几何验证**（以 24m×14m 建筑为例）:
   - ExtrudedDirection = `(0.,0.,1.)` ✓
   - Axis = `(1.,0.,0.)` ✓
   - 拉伸深度 = 25.2m（建筑宽度 + 悬挑）✓
   - 左坡面 p4 = `(0., 4.04)` — 屋脊中心 ✓
   - 右坡面 p4 = `(0., 4.04)` — 屋脊中心 ✓
   - 缝隙 = 0m ✓
3. **单元测试**: 85 个测试通过（7 个预存失败与本次修复无关）

## 变更文件

| 文件 | 变更类型 | 说明 |
|---|---|---|
| `app/pipeline.py` | 修改 | 修复 `_add_gable_slope_shape()` 中的拉伸方向和屋脊闭合 |
