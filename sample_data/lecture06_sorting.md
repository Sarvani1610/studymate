# Lecture 6: Sorting

## Comparison Sorting

A comparison sort orders elements only by comparing pairs of them. Any comparison sort needs at least on the order of n log n comparisons in the worst case, which is the lower bound for this family of algorithms.

A stable sort keeps equal elements in the same relative order they had in the input. Stability matters when sorting records by one field after they were already sorted by another field.

## Merge Sort

Merge sort is a divide and conquer algorithm that splits the array in half, sorts each half recursively and merges the two sorted halves. Merge sort always runs in O(n log n) time and is stable, but the standard array version needs O(n) extra memory for merging.

## Quicksort

Quicksort picks a pivot, partitions the array into elements smaller and larger than the pivot, and recursively sorts both sides. Quicksort runs in O(n log n) time on average and sorts in place. Quicksort degrades to quadratic time when the pivot is repeatedly the smallest or largest element, for example when the input is already sorted and the first element is used as the pivot. Choosing a random pivot or the median of three makes the bad case very unlikely.

## Heapsort

Heapsort builds a max heap from the input and repeatedly extracts the maximum. Heapsort runs in O(n log n) time in the worst case and sorts in place, but it is not a stable sort.

## Amortized Analysis

Amortized analysis bounds the average cost of an operation over a worst case sequence of operations. A dynamic array that doubles its capacity when full has O(1) amortized append time, even though a single append that triggers a resize costs O(n).
