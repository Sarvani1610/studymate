# Lecture 4: Trees and Heaps

## Binary Search Trees

A binary search tree is a binary tree where every node's left subtree holds smaller keys and the right subtree holds larger keys. Search, insert and delete run in O(h) time where h is the height of the tree. When the tree is balanced the height is O(log n), but inserting keys in sorted order turns the tree into something that behaves like a linked list with height n.

In-order traversal of a binary search tree visits the keys in sorted order. Deleting a node with two children replaces it with its in-order successor, which is the smallest key in the right subtree.

## Balanced Trees

A tree is balanced when the heights of the left and right subtrees of every node differ by a small bounded amount. Self balancing trees such as AVL trees and red black trees keep the height logarithmic no matter what order keys arrive in.

An AVL tree is a binary search tree that stores a balance factor at each node and performs rotations after inserts and deletes. A single rotation fixes the left-left and right-right cases, while the left-right and right-left cases need a double rotation.

A red black tree is a binary search tree that colors nodes red or black. The rules guarantee that no root to leaf path is more than twice as long as any other, so the height stays O(log n). Red black trees do fewer rotations than AVL trees on insert, which is why many standard libraries use them for ordered maps.

## Heaps and Priority Queues

A binary heap is a complete binary tree that satisfies the heap property: in a min heap every parent is less than or equal to its children. Heaps are stored in an array, where the children of index i live at 2i + 1 and 2i + 2. Insert and extract-min both run in O(log n) because they sift an element up or down one level at a time. Building a heap from an unsorted array takes O(n) with the bottom up heapify method.

A priority queue is an abstract data type that always returns the highest priority element first. Priority queues are commonly implemented with binary heaps. Dijkstra's algorithm uses a priority queue to pick the closest unvisited vertex on every step.
