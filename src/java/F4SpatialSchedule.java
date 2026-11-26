package com.xilinx.rapidwright.rwroute;

import java.util.ArrayList;
import java.util.List;

/** Small live windows, with the same left / crossing / right geometry as F5. */
final class F4SpatialSchedule {
    private final F1Corridor.Box[] boxes;
    private final int[] leaf;
    private int nextLeaf;

    F4SpatialSchedule(List<Connection> connections, List<F1Corridor.Box> proposed) {
        int count = connections.size();
        boxes = new F1Corridor.Box[count];
        for (int i = 0; i < count; ++i)
            boxes[i] = proposed.get(i) == null ? F1Corridor.Box.original(connections.get(i)) : proposed.get(i);
        leaf = new int[count];
        List<Integer> work = new ArrayList<>(count);
        for (int i = 0; i < count; ++i) work.add(i);
        partition(work);
    }

    int prefix() {
        for (int end = 1; end < leaf.length; ++end) {
            for (int i = 0; i < end; ++i) {
                if (leaf[i] == leaf[end] || overlaps(box(i), box(end))) return end;
            }
        }
        return leaf.length;
    }

    boolean unsplittable() { return leaf.length > 1 && nextLeaf == 1; }

    private F1Corridor.Box box(int index) { return boxes[index]; }

    private static boolean overlaps(F1Corridor.Box a, F1Corridor.Box b) {
        return a.left <= b.right && b.left <= a.right && a.bottom <= b.top && b.bottom <= a.top;
    }

    private void partition(List<Integer> members) {
        if (members.isEmpty()) return;
        int best = 0;
        int cut = 0;
        boolean xAxis = false;
        for (boolean x : new boolean[]{true, false}) {
            int maxOfMin = Integer.MIN_VALUE;
            int minOfMax = Integer.MAX_VALUE;
            for (int member : members) {
                maxOfMin = Math.max(maxOfMin, min(box(member), x));
                minOfMax = Math.min(minOfMax, max(box(member), x));
            }
            if (maxOfMin <= minOfMax) continue;
            for (int member : members) {
                int candidate = max(box(member), x);
                int left = 0;
                int right = 0;
                for (int next : members) {
                    if (max(box(next), x) <= candidate) ++left;
                    else if (min(box(next), x) > candidate) ++right;
                }
                if (left == 0 || right == 0) continue;
                int score = (left + right) * leaf.length + Math.min(left, right);
                if (score > best) {
                    best = score;
                    cut = candidate;
                    xAxis = x;
                }
            }
        }
        if (best == 0) {
            int id = nextLeaf++;
            for (int member : members) leaf[member] = id;
            return;
        }
        List<Integer> left = new ArrayList<>();
        List<Integer> crossing = new ArrayList<>();
        List<Integer> right = new ArrayList<>();
        for (int item : members) {
            F1Corridor.Box connection = box(item);
            if (max(connection, xAxis) <= cut) left.add(item);
            else if (min(connection, xAxis) > cut) right.add(item);
            else crossing.add(item);
        }
        partition(left);
        partition(crossing);
        partition(right);
    }

    private static int min(F1Corridor.Box connection, boolean x) {
        return x ? connection.left : connection.bottom;
    }

    private static int max(F1Corridor.Box connection, boolean x) {
        return x ? connection.right : connection.top;
    }
}
