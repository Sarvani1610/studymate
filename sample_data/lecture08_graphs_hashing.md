# Lecture 8: Graphs and Hashing

## Graph Traversal

Breadth first search explores a graph level by level using a queue. BFS finds shortest paths in unweighted graphs because it reaches every vertex at distance k before any vertex at distance k + 1.

Depth first search goes as deep as possible along each branch before backtracking, using a stack or recursion. DFS is the basis for topological sort, cycle detection and finding strongly connected components.

The difference between BFS and DFS is mostly the order of exploration: BFS uses a queue and explores by distance, while DFS uses a stack and explores by depth. Both run in O(V + E) time on an adjacency list.

## Shortest Paths

Dijkstra's algorithm finds shortest paths from one source in a graph with non negative edge weights. It keeps a priority queue of tentative distances and repeatedly settles the closest unvisited vertex. With a binary heap it runs in O((V + E) log V) time. Negative edge weights break Dijkstra's algorithm, and Bellman-Ford is used instead.

## Hash Tables

A hash table maps keys to buckets using a hash function. Collisions are handled by chaining, where each bucket holds a list, or by open addressing, where the table probes for another empty slot.

The load factor of a hash table is the number of stored entries divided by the number of buckets. When the load factor gets too high the table is resized and every key is rehashed, which keeps the expected lookup time at O(1).
