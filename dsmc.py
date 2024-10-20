import numpy as np
import matplotlib.pyplot as plt
import argparse
import tqdm

import tf_keras
import tensorflow_probability as tfp


tfb = tfp.bijectors
tfd = tfp.distributions
tfpl = tfp.layers


class DSMCSimulation:
    def __init__(self, n_particles, n_steps, time_step=1e-6, use_mdn=False, mdn_model=None, T_tr_initial=380, T_rot_initial=180, Z_r=245, domain_size=6.4e-4, n_cells=10, sigma_collision=2.92e-10):
        # Simulation parameters
        self.n_particles = n_particles
        self.n_steps = n_steps
        self.T_tr_initial = T_tr_initial
        self.T_rot_initial = T_rot_initial
        self.Z_r = Z_r
        self.p_inelastic = 1 - (1/self.Z_r)  # Inelastic collision probability
        self.domain_size = domain_size
        self.n_cells = n_cells
        self.sigma_collision = sigma_collision


        self.k_B = 1.38e-23  # Boltzmann constant (J/K)
        self.m_H2 = 3.34e-26  # Mass of hydrogen molecule (kg)
        self.density = 0.9  # Density of particles (kg/m^3)

        # v_init = np.sqrt(3*boltz*T/mass)
        self.v_init = np.sqrt(3 * self.k_B * self.T_tr_initial / self.m_H2)  # Initial velocity
        #tau = 0.2*(L/ncell)/v_init
        self.time_step = 0.2 * (self.domain_size / self.n_cells) / self.v_init   # Set timestep (in seconds)
        # eff_num = density/mass * L**3 /npart
        self.eff_num = self.density/self.m_H2 * domain_size**3 / n_particles
        # coeff = 0.5*eff_num*np.pi*diam**2*tau/(L**3/ncell)
        self.coeff = 0.5 * self.eff_num * np.pi * self.sigma_collision**2 * self.time_step / (self.domain_size**3 / self.n_cells)

        # Option to use the MDN-based surrogate model
        self.use_mdn = use_mdn
        self.mdn_model = mdn_model  # The MDN model passed during initialization
        
        # Initialize arrays for positions, velocities, and energies
        self.positions = self.initialize_positions()
        self.velocities = self.initialize_velocities(self.T_tr_initial)
        self.rotational_energy = 0.5 * self.k_B * self.T_rot_initial * np.ones(self.n_particles)
        
        # Initialize spatial cells
        self.cells = np.zeros((self.n_cells, self.n_cells, self.n_cells), dtype=object)
        self.cell_size = self.domain_size / self.n_cells
        self.volume_cell = self.cell_size ** 3  # Volume of each cell
        
        # Energy history for plotting
        self.translational_energy_history = []
        self.rotational_energy_history = []
        self.total_energy_history = []
        self.elastic_collisions = 0
        self.inelastic_collisions = 0
        self.rejected_collisions = 0
        

    def initialize_velocities(self, T):
        """Initialize velocities based on Maxwell-Boltzmann distribution."""
        return np.random.normal(0, np.sqrt(self.k_B * T / self.m_H2), (self.n_particles, 3))

    def initialize_positions(self):
        """Initialize positions of particles randomly in the domain."""
        return np.random.rand(self.n_particles, 3) * self.domain_size

    def initialize_cells(self):
        """Initialize empty cells for particles."""
        self.cells = np.zeros((self.n_cells, self.n_cells, self.n_cells), dtype=object)
        for i in range(self.n_cells):
            for j in range(self.n_cells):
                for k in range(self.n_cells):
                    self.cells[i, j, k] = []

    def assign_to_cells(self):
        """Assign particles to cells based on their positions."""
        self.initialize_cells()  # Reset the cells before assignment
        for i in range(self.n_particles):
            cell_indices = (self.positions[i] // self.cell_size).astype(int)
            x, y, z = cell_indices
            self.cells[x, y, z].append(i)

    def compute_kinetic_energy(self, velocity):
        """Compute the kinetic energy of a particle."""
        return 0.5 * self.m_H2 * np.sum(velocity**2)

    def sigmoid(self, x):
        """Sigmoid function."""
        return 1 / (1 + np.exp(-x))

    def inv_sigmoid(self, x):
        """Inverse sigmoid function with clamping to avoid division by zero."""
        epsilon = 1e-9  # Small value to avoid log(0) or division by zero
        x = np.clip(x, epsilon, 1 - epsilon)  # Ensure x is within (0, 1)
        return np.log(x / (1 - x))


    def mdn_energy_exchange(self, pre_collisional_energies):
        """Use the trained MDN to predict post-collisional energies."""
        # Input format: [log(E_c), inv_sigmoid(eps_t), inv_sigmoid(eps_r1)]
        log_Ec, inv_eps_t, inv_eps_r1 = pre_collisional_energies
        input_data = np.array([[log_Ec, inv_eps_t, inv_eps_r1]])

        # Perform prediction
        predictions = self.mdn_model.predict(input_data, verbose=0)

        # Extract predictions and transform back
        inv_eps_tp, inv_eps_r1p = predictions[0]
        #Ec_post = np.exp(pre_collisional_energies[0])       #post total is precolissional total
        eps_t_post = self.sigmoid(inv_eps_tp)
        eps_r1_post = self.sigmoid(inv_eps_r1p)

        return eps_t_post, eps_r1_post
    
    def perform_collision(self, idx1, idx2 , max_rel_velocity):
        """Handle the collision between two particles using the regular Larsen-Borgnakke model."""
        velocity1, velocity2 = self.velocities[idx1], self.velocities[idx2]
        relative_velocity = self.calculate_relative_velocity(velocity1, velocity2)
        CM_velocity = 0.5 * (velocity1 + velocity2)

        # Compute collision probability using fixed v_rel_max
        collision_prob = np.linalg.norm(relative_velocity) / max_rel_velocity
        if np.random.rand() > collision_prob:
            self.rejected_collisions += 1
            return  # Skip collision if it does not happen based on collision probability

        # Test for inelastic collision for the collision pair
        if np.random.rand() > self.p_inelastic:  # Elastic collision
            self.elastic_collisions += 1

            # Isotropic scattering (randomize the relative velocity direction)
            relative_speed = np.linalg.norm(relative_velocity)
            new_relative_velocity = self.random_unit_vector() * relative_speed

            # Update velocities
            self.velocities[idx1] = CM_velocity + 0.5 * new_relative_velocity
            self.velocities[idx2] = CM_velocity - 0.5 * new_relative_velocity
            return

        else:  # Inelastic collision using regular Larsen-Borgnakke model
            self.inelastic_collisions += 1

            # Total energy before collision
            E_trans_pre = 0.5 * self.m_H2 * np.sum((velocity1 - CM_velocity) ** 2 + (velocity2 - CM_velocity) ** 2)
            E_rot_pre = self.rotational_energy[idx1] + self.rotational_energy[idx2]
            E_total_pre = E_trans_pre + E_rot_pre

            # Degrees of freedom
            dof_trans = 3  # 3 translational degrees of freedom
            dof_rot = 2    # 2 rotational degrees of freedom for diatomic molecules

            # Redistribute total energy among translational and rotational modes using the beta distribution
            energy_fraction_trans = np.random.beta(dof_trans / 2, dof_rot / 2)
            E_trans_post = E_total_pre * energy_fraction_trans
            E_rot_post = E_total_pre - E_trans_post

            # Distribute rotational energy equally between the two molecules
            self.rotational_energy[idx1] = E_rot_post / 2
            self.rotational_energy[idx2] = E_rot_post / 2

            # Correct calculation of relative speed
            relative_speed = np.sqrt(4 * E_trans_post / self.m_H2)
            new_relative_velocity = self.random_unit_vector() * relative_speed

            # Update velocities
            self.velocities[idx1] = CM_velocity + 0.5 * new_relative_velocity
            self.velocities[idx2] = CM_velocity - 0.5 * new_relative_velocity

            # Energy conservation check
            E_trans_post_check = 0.5 * self.m_H2 * np.sum((self.velocities[idx1] - CM_velocity) ** 2 + (self.velocities[idx2] - CM_velocity) ** 2)
            E_rot_post_check = self.rotational_energy[idx1] + self.rotational_energy[idx2]
            E_total_post = E_trans_post_check + E_rot_post_check

            assert np.isclose(E_total_pre, E_total_post, atol=1e-10), "Energy conservation violated!"



    def random_unit_vector(self):
        """Generate a random unit vector uniformly distributed over the sphere."""
        theta = np.arccos(1 - 2 * np.random.rand())
        phi = 2 * np.pi * np.random.rand()
        x = np.sin(theta) * np.cos(phi)
        y = np.sin(theta) * np.sin(phi)
        z = np.cos(theta)
        return np.array([x, y, z])

    def max_relative_velocity_in_cell(self, particles_in_cell):
        """Calculate the maximum relative velocity (magnitude) among all pairs in a cell."""
        max_rel_velocity = 0
        for i in range(len(particles_in_cell)):
            for j in range(i + 1, len(particles_in_cell)):
                idx1, idx2 = particles_in_cell[i], particles_in_cell[j]
                rel_velocity = self.calculate_relative_velocity(self.velocities[idx1], self.velocities[idx2])

                # Compare the magnitude (norm) of the relative velocity vectors
                rel_velocity_magnitude = np.linalg.norm(rel_velocity)
                if rel_velocity_magnitude > max_rel_velocity:
                    max_rel_velocity = rel_velocity_magnitude

        return max_rel_velocity

    def calculate_relative_velocity(self, vel1, vel2):
        """Calculate the relative velocity between two particles."""
        relative_velocity = vel1 - vel2
        return relative_velocity

    def calculate_total_energy(self):
        """Calculate the total translational and rotational energy in the system."""
        total_kinetic_energy = np.sum([self.compute_kinetic_energy(vel) for vel in self.velocities])
        total_rotational_energy = np.sum(self.rotational_energy)
        total_energy = (total_kinetic_energy  + total_rotational_energy)/1
        return total_kinetic_energy, total_rotational_energy , total_energy

    def update_positions(self):
        """Update particle positions based on velocity and timestep, applying periodic boundary conditions."""
        self.positions += self.velocities * self.time_step
        
        # Apply periodic boundary conditions
        self.positions = np.mod(self.positions, self.domain_size) # PERIODIC BOUNDARY CONDITIONS

    def plot_positions(self):
        """Plot the positions of particles in 3D."""
        fig = plt.figure(figsize=(10, 8))
        ax = fig.add_subplot(111, projection='3d')
        ax.scatter(self.positions[:, 0], self.positions[:, 1], self.positions[:, 2])
        ax.set_xlabel("X")
        ax.set_ylabel("Y")
        ax.set_zlabel("Z")
        ax.set_title("Particle Positions")
        plt.show()

    def dsmc_step(self):
        """Perform one step of BL-DSMC simulation, including particle collisions within cells and updating positions."""
        # Update particle positions based on velocities and timestep
        self.update_positions()

        # Re-assign particles to cells after position update
        self.assign_to_cells()

        # Loop over cells to perform collisions within each cell
        for i in range(self.n_cells):
            for j in range(self.n_cells):
                for k in range(self.n_cells):
                    cell_particles = self.cells[i, j, k]
                    n_cell_particles = len(cell_particles)

                    if n_cell_particles < 2:
                        continue  # No collisions in a cell with less than 2 particles

                    # Calculate the maximum relative velocity in the cell
                    max_rel_velocity = self.max_relative_velocity_in_cell(cell_particles)

                    # Calculate number of candidate collision pairs to be selected (based on the original code)
                    # select = coeff*number*(number-1)*crmax[jcell] 
                    n_candidate_pairs = int(self.coeff * n_cell_particles * (n_cell_particles - 1) * max_rel_velocity)
                    print(f"Cell ({i}, {j}, {k}): {n_cell_particles} particles, {n_candidate_pairs} candidate pairs") 
                    # Print all the parameters for debugging

                    # Perform candidate collisions
                    for _ in range(n_candidate_pairs):
                        idx1, idx2 = np.random.choice(cell_particles, 2, replace=False)
                        self.perform_collision(idx1, idx2, max_rel_velocity)

    def run_simulation(self, mode='full'):
        """Main simulation loop."""
        # Reset energy history
        self.translational_energy_history = []
        self.rotational_energy_history = []

        # Run the simulation for the defined number of steps
        with tqdm.tqdm(total=self.n_steps) as pbar:
            for step in range(self.n_steps):
                pbar.set_description(f"Step {step}")
                self.dsmc_step()

                # Calculate total translational and rotational energy
                total_kinetic_energy, total_rotational_energy, total_energy = self.calculate_total_energy()
                self.translational_energy_history.append(total_kinetic_energy)
                self.rotational_energy_history.append(total_rotational_energy)
                self.total_energy_history.append(total_energy)


                # For quick validation, print the energy at every few steps in test mode
                if mode == 'test' and step % 100 == 0:
                    print(f"Step {step}: Translational Energy = {total_kinetic_energy}, Rotational Energy = {total_rotational_energy}")

                pbar.update(1)

        print("Simulation complete.")

    def plot_energy_relaxation(self, mode='full'):
        """Plot the energy relaxation over time."""
        plt.figure(figsize=(10, 6))
        plt.plot(self.translational_energy_history, label="Translational Energy", color='b')
        plt.plot(self.rotational_energy_history, label="Rotational Energy", color='r')
        plt.plot(self.total_energy_history, label="Total Energy", color='k')
        plt.title(f"Energy Relaxation in DSMC ({mode} mode)")
        plt.xlabel("Time Step")
        plt.ylabel("Energy (J)")
        plt.legend()
        plt.show()

    def plot_temperature_relaxation(self):
        """Plot the temperature relaxation over time."""
        # Degrees of freedom
        dof_trans = 3
        dof_rot = 2
        k_B = self.k_B
        N = self.n_particles

        # Time axis
        time_steps = np.arange(self.n_steps) * self.time_step

        # Convert energies to temperatures
        T_trans = np.array(self.translational_energy_history) / (0.5 * N * k_B * dof_trans)
        T_rot = np.array(self.rotational_energy_history) / (0.5 * N * k_B * dof_rot)
        T_total = np.array(self.total_energy_history) / (0.5 * N * k_B * (dof_trans + dof_rot))

        plt.figure(figsize=(10, 6))
        plt.plot(time_steps, T_trans, label="Translational Temperature", color='b')
        plt.plot(time_steps, T_rot, label="Rotational Temperature", color='r')
        plt.plot(time_steps, T_total, label="Total Temperature", color='k')
        plt.title("Temperature Relaxation in DSMC Simulation")
        plt.xlabel("Time (s)")
        plt.ylabel("Temperature (K)")
        plt.legend()
        plt.show()



if __name__ == "__main__":
    # Load the trained MDN model here (assuming TensorFlow model)
    parser = argparse.ArgumentParser(description="DSMC Simulation")

    parser.add_argument("--mdn", type=str, default=None, help="Path to the trained MDN model")
    parser.add_argument("--n_particles", type=int, default=50000, help="Number of particles")
    parser.add_argument("--n_steps", type=int, default=1000, help="Number of steps")
    parser.add_argument("--time_step", type=float, default=1e-6, help="Timestep in seconds")

    args = parser.parse_args()

    if args.mdn: ##--mdn path
        print("Using MDN model for energy exchange.")
        use_mdn = True
        
        def build_model(NGAUSSIANS, ACTIVATION, NNEURONS):
            
            event_shape = [2]
            num_components = NGAUSSIANS
            params_size = tfpl.MixtureSameFamily.params_size(num_components,
                            component_params_size=tfpl.IndependentNormal.params_size(event_shape))

            negloglik = lambda y, p_y: -p_y.log_prob(y)

            model = tf_keras.models.Sequential([
                tf_keras.layers.Dense(NNEURONS, activation=ACTIVATION),
                tf_keras.layers.Dense(params_size, activation=None),
                tfpl.MixtureSameFamily(num_components, tfpl.IndependentNormal(event_shape)),
            ])
            
            model.compile(optimizer=tf_keras.optimizers.Adam(learning_rate = 1e-4), loss=negloglik)

            return model


        mdn_model = build_model(20, 'relu', 8) #settings have to match otherwise the loaded weights won't wrork.

        #needed initialization step, probably determines the input size from here.
        mdn_model(np.ones((3,3)))

        mdn_model.load_weights(args.mdn)
        mdn_model.summary()
        
    else:
        use_mdn = False
        mdn_model = None

    dsmc = DSMCSimulation(n_particles=args.n_particles, n_steps=args.n_steps, time_step=args.time_step, use_mdn=use_mdn, mdn_model=mdn_model)
    dsmc.run_simulation()
    print(f"Total elastic collisions: {dsmc.elastic_collisions}")
    print(f"Total inelastic collisions: {dsmc.inelastic_collisions}")
    print(f"Total rejected collisions percentage: {dsmc.rejected_collisions/ (dsmc.elastic_collisions + dsmc.inelastic_collisions + dsmc.rejected_collisions) * 100}%")
    dsmc.plot_energy_relaxation()
    dsmc.plot_temperature_relaxation()
    dsmc.plot_positions()