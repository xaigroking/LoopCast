// SPDX-License-Identifier: Apache-2.0
import type { ChildProcess } from 'node:child_process';

export interface LaunchSpec {
  executable: string;
  argv: string[];
  cwd: string;
  env: Record<string, string>;
  stdio: ('pipe' | 'ignore')[];
  /** performance.now() domain, not Date.now(). Must include process creation. */
  deadline: number;
  signal: AbortSignal;
}
export interface OwnedTree {
  /** Pipes must be attached before the child can emit data or exit events. */
  child: ChildProcess;
  /**
   * Must act on the owned complete tree even after leader exit or an expired
   * deadline, without resetting that deadline. confirmed means every descendant
   * and pipe pump has ended. May force termination; this is separate from the
   * application's STOPPED/exit0 business shutdown acknowledgement.
   */
  closeTree(deadline: number): Promise<{ confirmed: boolean }>;
}
export interface TreePort {
  format: 'loopcast.process-tree.v1';
  /**
   * Validate private data/session directories and build minimal environments.
   * Reject unresolved cleanup/recovery barriers from an earlier host, retaining
   * the original data and recovery facts. Never clear a lock or fence to start.
   */
  environment(paths: { dataDir: string; sessionDir: string; deadline: number; signal: AbortSignal }): Promise<{
    api: Record<string, string>;
    web: Record<string, string>;
  }>;
  /**
   * Synchronous ownership transfer. Failure must clean anything created before
   * throwing. Windows children must join their Job before executing user code.
   * Parent death/pipe EOF handling belongs to the platform port.
   */
  /** Fixed offline environment; no owner preflight, inherited credentials or arbitrary environment forwarding. */
  maintenanceEnvironment?(): Record<string, string>;
  /** Linux guardians receive a bounded private admission frame on fd6 and forward it only to fixed Python entry modules. */
  launch(spec: LaunchSpec): OwnedTree;
  /** Clear only this launch's runtime guard after every tree and API shutdown. */
  finish?(result: { confirmed: boolean; deadline: number }): Promise<{ confirmed: boolean }>;
}
